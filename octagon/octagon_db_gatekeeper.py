"""octagon_db_gatekeeper.py — adapted from nex_db_gatekeeper.py v3.

Changes from NEX original:
- _CHECKPOINT_DB_PATH is configurable via set_checkpoint_db() rather than hardcoded to nex.db
- Module docstring updated; all logic identical to v3
"""

import sqlite3
import threading
import logging
import re
import time

__all__ = ["install", "set_checkpoint_db", "STATS", "LOCK_TIMEOUT_S", "LOCK_RETRY_ATTEMPTS"]
log = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────
LOCK_TIMEOUT_S = 10
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_SLEEP_S = 0.1
WATCHDOG_INTERVAL_S = 10
WATCHDOG_WARN_THRESHOLD_S = 30
SQLITE_BLOCK_THRESHOLD_S = 5

# ── Checkpoint DB path (set by ledger before first use) ──────────────
_CHECKPOINT_DB_PATH: str = ""


def set_checkpoint_db(path: str) -> None:
    global _CHECKPOINT_DB_PATH
    _CHECKPOINT_DB_PATH = path


# ── Lock + owner tracking ────────────────────────────────────────────
_WRITE_LOCK = threading.RLock()
_LOCK_OWNER: dict = {"tid": None, "sql": None, "acquired_at": None, "depth": 0}
_LOCK_OWNER_LOCK = threading.Lock()

STATS: dict = {
    "connections_created": 0,
    "writes_serialized": 0,
    "reads_passed_through": 0,
    "lock_waits_total_ms": 0.0,
    "max_lock_wait_ms": 0.0,
    "lock_timeouts": 0,
    "watchdog_warnings": 0,
    "sqlite_blocked_events": 0,
}

_WRITE_RE = re.compile(
    r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*"
    r"(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE|BEGIN|COMMIT|ROLLBACK|VACUUM|REINDEX|ANALYZE)",
    re.IGNORECASE | re.DOTALL,
)


def _is_write(sql: object) -> bool:
    if not isinstance(sql, str):
        return False
    return bool(_WRITE_RE.match(sql))


def _track_wait(t0: float) -> None:
    elapsed_ms = (time.perf_counter() - t0) * 1000
    STATS["lock_waits_total_ms"] += elapsed_ms
    if elapsed_ms > STATS["max_lock_wait_ms"]:
        STATS["max_lock_wait_ms"] = elapsed_ms


def _acquire_write_lock(sql: str) -> None:
    tid = threading.get_ident()
    for attempt in range(LOCK_RETRY_ATTEMPTS):
        if _WRITE_LOCK.acquire(timeout=LOCK_TIMEOUT_S):
            with _LOCK_OWNER_LOCK:
                if _LOCK_OWNER["tid"] == tid and _LOCK_OWNER["depth"] > 0:
                    _LOCK_OWNER["depth"] += 1
                else:
                    _LOCK_OWNER["tid"] = tid
                    _LOCK_OWNER["sql"] = sql[:80] if sql else None
                    _LOCK_OWNER["acquired_at"] = time.time()
                    _LOCK_OWNER["depth"] = 1
            return
        if attempt < LOCK_RETRY_ATTEMPTS - 1:
            time.sleep(LOCK_RETRY_SLEEP_S)

    with _LOCK_OWNER_LOCK:
        holder_tid = _LOCK_OWNER.get("tid")
        holder_sql = _LOCK_OWNER.get("sql")
        holder_acquired_at = _LOCK_OWNER.get("acquired_at")
    held_for = (time.time() - holder_acquired_at) if holder_acquired_at else None
    STATS["lock_timeouts"] += 1
    raise TimeoutError(
        f"gatekeeper: write lock unavailable after "
        f"{LOCK_RETRY_ATTEMPTS}x{LOCK_TIMEOUT_S}s. "
        f"holder tid={holder_tid} sql={holder_sql!r} held_for={held_for}s. "
        f"attempted sql={(sql[:80] if sql else None)!r}"
    )


def _release_write_lock() -> None:
    with _LOCK_OWNER_LOCK:
        if _LOCK_OWNER["depth"] > 1:
            _LOCK_OWNER["depth"] -= 1
        else:
            _LOCK_OWNER["tid"] = None
            _LOCK_OWNER["sql"] = None
            _LOCK_OWNER["acquired_at"] = None
            _LOCK_OWNER["depth"] = 0
    _WRITE_LOCK.release()


def _watchdog_loop() -> None:
    checkpoint_counter = 0
    while True:
        try:
            time.sleep(WATCHDOG_INTERVAL_S)
            with _LOCK_OWNER_LOCK:
                holder_tid = _LOCK_OWNER.get("tid")
                holder_acquired_at = _LOCK_OWNER.get("acquired_at")
                holder_sql = _LOCK_OWNER.get("sql")

            if holder_tid is not None and holder_acquired_at is not None:
                held = time.time() - holder_acquired_at
                if held > WATCHDOG_WARN_THRESHOLD_S:
                    STATS["watchdog_warnings"] += 1
                    log.warning(
                        "[gatekeeper watchdog] write lock held %.1fs by tid=%s sql=%r",
                        held,
                        holder_tid,
                        holder_sql,
                    )

            checkpoint_counter += 1
            if checkpoint_counter >= 6:
                checkpoint_counter = 0
                if holder_tid is None and _CHECKPOINT_DB_PATH:
                    try:
                        import os
                        conn = sqlite3.connect(
                            os.path.expanduser(_CHECKPOINT_DB_PATH), timeout=2
                        )
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                        conn.close()
                    except Exception as e:
                        log.debug("[gatekeeper watchdog] checkpoint skip: %s", e)
        except Exception as e:
            log.exception("[gatekeeper watchdog] internal error: %s", e)


class _GatedCursor(sqlite3.Cursor):
    def execute(self, sql: str, *args, **kwargs):  # type: ignore[override]
        if _is_write(sql):
            t0 = time.perf_counter()
            _acquire_write_lock(sql)
            try:
                _track_wait(t0)
                STATS["writes_serialized"] += 1
                t_exec = time.perf_counter()
                raised = False
                try:
                    return super().execute(sql, *args, **kwargs)
                except BaseException:
                    raised = True
                    raise
                finally:
                    elapsed = time.perf_counter() - t_exec
                    if elapsed > SQLITE_BLOCK_THRESHOLD_S:
                        STATS["sqlite_blocked_events"] += 1
                        log.warning(
                            "[gatekeeper sqlite-blocked] execute %.1fs status=%s tid=%s sql=%r",
                            elapsed,
                            "raised" if raised else "ok",
                            threading.get_ident(),
                            sql[:80] if sql else "",
                        )
            finally:
                _release_write_lock()
        STATS["reads_passed_through"] += 1
        return super().execute(sql, *args, **kwargs)

    def executemany(self, sql: str, *args, **kwargs):  # type: ignore[override]
        if _is_write(sql):
            t0 = time.perf_counter()
            _acquire_write_lock(sql)
            try:
                _track_wait(t0)
                STATS["writes_serialized"] += 1
                return super().executemany(sql, *args, **kwargs)
            finally:
                _release_write_lock()
        STATS["reads_passed_through"] += 1
        return super().executemany(sql, *args, **kwargs)


class _GatedConnection(sqlite3.Connection):
    def cursor(self, *args, **kwargs):  # type: ignore[override]
        if args or "factory" in kwargs:
            return super().cursor(*args, **kwargs)
        return super().cursor(_GatedCursor)

    def execute(self, sql: str, *args, **kwargs):  # type: ignore[override]
        if _is_write(sql):
            t0 = time.perf_counter()
            _acquire_write_lock(sql)
            try:
                _track_wait(t0)
                STATS["writes_serialized"] += 1
                t_exec = time.perf_counter()
                raised = False
                try:
                    return super().execute(sql, *args, **kwargs)
                except BaseException:
                    raised = True
                    raise
                finally:
                    elapsed = time.perf_counter() - t_exec
                    if elapsed > SQLITE_BLOCK_THRESHOLD_S:
                        STATS["sqlite_blocked_events"] += 1
                        log.warning(
                            "[gatekeeper sqlite-blocked] execute %.1fs status=%s tid=%s sql=%r",
                            elapsed,
                            "raised" if raised else "ok",
                            threading.get_ident(),
                            sql[:80] if sql else "",
                        )
            finally:
                _release_write_lock()
        STATS["reads_passed_through"] += 1
        return super().execute(sql, *args, **kwargs)

    def executemany(self, sql: str, *args, **kwargs):  # type: ignore[override]
        if _is_write(sql):
            t0 = time.perf_counter()
            _acquire_write_lock(sql)
            try:
                _track_wait(t0)
                STATS["writes_serialized"] += 1
                return super().executemany(sql, *args, **kwargs)
            finally:
                _release_write_lock()
        STATS["reads_passed_through"] += 1
        return super().executemany(sql, *args, **kwargs)


def install() -> None:
    if getattr(sqlite3, "_gatekept", False):
        return
    real_connect = sqlite3.connect
    sqlite3._real_connect = real_connect  # type: ignore[attr-defined]

    def gatekept_connect(*args, **kwargs):
        STATS["connections_created"] += 1
        if "factory" not in kwargs:
            kwargs["factory"] = _GatedConnection
        conn = real_connect(*args, **kwargs)
        try:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            log.warning("gatekeeper: PRAGMA setup failed: %s", e)
        return conn

    sqlite3.connect = gatekept_connect  # type: ignore[assignment]
    sqlite3._gatekept = True  # type: ignore[attr-defined]

    _wd = threading.Thread(
        target=_watchdog_loop, daemon=True, name="octagon-gatekeeper-watchdog"
    )
    _wd.start()

    _logging_status = (
        "logging configured"
        if logging.getLogger().hasHandlers()
        else "logging not configured (lastResort stderr)"
    )
    print(
        f"[octagon_db_gatekeeper] v3 installed — bounded acquire "
        f"({LOCK_RETRY_ATTEMPTS}x{LOCK_TIMEOUT_S}s) + watchdog ({_logging_status})"
    )


install()
