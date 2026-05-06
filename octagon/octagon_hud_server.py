"""
octagon_hud_server.py — read-only HUD server for the Octagon dashboard.

Runs on localhost:7711 by default. Three endpoints:
  GET  /             — dashboard HTML
  GET  /logo.svg     — Octogon mark
  GET  /api/state    — current HUD state as JSON (one-shot)
  GET  /sse/state    — SSE stream, pushes state every 2s

Reads octagon.db directly in read-only mode. Safe to run alongside the
main daemon — WAL mode handles concurrent reader+writer.

Run:  python -m octagon.octagon_hud_server
Or:   python -m octagon.hud         (if a thin __main__.py wrapper exists)
"""

from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog

from octagon.octagon_config import CONFIG, FREQ_LEVER_PATH, TIERS, get_effective_tier
import octagon.octagon_hype_detector as hype_detector
import octagon.octagon_copy_trader as copy_trader

log = structlog.get_logger(__name__)

HUD_HOST = os.getenv("HUD_HOST", "127.0.0.1")
HUD_PORT = int(os.getenv("HUD_PORT", "7711"))
SSE_INTERVAL_SECONDS = float(os.getenv("HUD_SSE_INTERVAL", "2.0"))

HUD_DIR = Path(__file__).parent / "hud"
_START_TIME = time.time()


# ── State assembly ────────────────────────────────────────────────────────────


def _ro_connect() -> sqlite3.Connection | None:
    db_path = Path(CONFIG.db_path).expanduser()
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def _read_state() -> dict[str, Any]:
    con = _ro_connect()
    if con is None:
        return _empty_state()
    try:
        return _build_state(con)
    finally:
        con.close()


def _build_state(con: sqlite3.Connection) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = (now - timedelta(days=7)).isoformat()
    day_ago = (now - timedelta(hours=24)).isoformat()

    n_markets = con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    n_predictions = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    n_today = con.execute(
        "SELECT COUNT(*) FROM predictions WHERE predicted_at >= ?",
        (today_start.isoformat(),),
    ).fetchone()[0]
    n_unresolved = con.execute(
        """
        SELECT COUNT(*) FROM markets m
        WHERE EXISTS (SELECT 1 FROM predictions p WHERE p.market_id = m.market_id)
        AND NOT EXISTS (SELECT 1 FROM resolutions r WHERE r.market_id = m.market_id)
        """
    ).fetchone()[0]

    cit = con.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN unciteable=0 THEN 1 ELSE 0 END), 0) AS citeable,
            COALESCE(SUM(CASE WHEN unciteable=1 THEN 1 ELSE 0 END), 0) AS unciteable,
            COUNT(*) AS total
        FROM predictions
        WHERE predicted_at >= ?
        """,
        (week_ago,),
    ).fetchone()
    citeable_rate = (cit["citeable"] / cit["total"]) if cit["total"] else 0.0

    edges_rows = con.execute(
        """
        SELECT m.question, m.category, p.p_yes,
               p.market_price_at_prediction AS px,
               (p.p_yes - p.market_price_at_prediction) AS edge
        FROM predictions p
        JOIN markets m ON p.market_id = m.market_id
        WHERE p.unciteable = 0
        AND p.predicted_at >= ?
        ORDER BY ABS(p.p_yes - p.market_price_at_prediction) DESC
        LIMIT 10
        """,
        (day_ago,),
    ).fetchall()

    recent_rows = con.execute(
        """
        SELECT m.question, p.p_yes, p.confidence, p.predicted_at, p.unciteable
        FROM predictions p
        JOIN markets m ON p.market_id = m.market_id
        ORDER BY p.predicted_at DESC
        LIMIT 12
        """
    ).fetchall()

    calib_cat_rows = con.execute(
        "SELECT category, brier, bias, n FROM calibration_category ORDER BY n DESC LIMIT 4"
    ).fetchall()

    src_calib_rows = con.execute(
        "SELECT source_url, brier_with, n_with FROM calibration_source ORDER BY n_with DESC LIMIT 10"
    ).fetchall()
    src_count_rows = con.execute(
        """
        SELECT source_url, COUNT(*) AS n
        FROM evidence_refs
        GROUP BY source_url
        ORDER BY n DESC
        LIMIT 10
        """
    ).fetchall()

    cite_buckets = []
    for d in range(6, -1, -1):
        ds = today_start - timedelta(days=d)
        de = ds + timedelta(days=1)
        row = con.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN unciteable=0 THEN 1 ELSE 0 END), 0) AS cit,
                COUNT(*) AS total
            FROM predictions
            WHERE predicted_at >= ? AND predicted_at < ?
            """,
            (ds.isoformat(), de.isoformat()),
        ).fetchone()
        rate = (row["cit"] / row["total"]) if row and row["total"] else 0.0
        cite_buckets.append({
            "date": ds.date().isoformat(),
            "rate": rate,
            "n": row["total"] if row else 0,
        })

    next_pending = con.execute(
        """
        SELECT m.question, m.category, m.resolves_at,
               (SELECT p.p_yes FROM predictions p WHERE p.market_id = m.market_id
                ORDER BY p.predicted_at DESC LIMIT 1) AS p_yes,
               (SELECT p.market_price_at_prediction FROM predictions p WHERE p.market_id = m.market_id
                ORDER BY p.predicted_at DESC LIMIT 1) AS px
        FROM markets m
        WHERE m.resolves_at > ?
        AND EXISTS (SELECT 1 FROM predictions WHERE market_id = m.market_id)
        AND NOT EXISTS (SELECT 1 FROM resolutions r WHERE r.market_id = m.market_id)
        ORDER BY m.resolves_at ASC
        LIMIT 1
        """,
        (now.isoformat(),),
    ).fetchone()

    res_rows = con.execute(
        """
        SELECT m.question, r.outcome,
               (SELECT p.p_yes FROM predictions p WHERE p.market_id = r.market_id
                ORDER BY p.predicted_at DESC LIMIT 1) AS p_yes,
               r.resolved_at
        FROM resolutions r
        JOIN markets m ON r.market_id = m.market_id
        ORDER BY r.resolved_at DESC
        LIMIT 8
        """
    ).fetchall()

    cycle = _approx_cycle_count(con)
    next_scan_seconds = _seconds_to_next_scan(con)

    bankroll = _compute_bankroll(con)
    freq_lever = _read_freq_lever_state(bankroll)

    return {
        "tick": int(time.time()),
        "system": {
            "cycle": cycle,
            "uptime_seconds": int(time.time() - _START_TIME),
            "phase": "phase 0",
            "scan_status": "active" if n_markets > 0 else "idle",
            "research_status": "active" if n_predictions > 0 else "idle",
            "ledger_status": "active",
            "exec_status": "disabled",
            "next_scan_seconds": next_scan_seconds,
            "loop_interval_seconds": int(getattr(CONFIG, "loop_interval_seconds", 900)),
            "predictions_today": n_today,
        },
        "total": {
            "paper_pnl": bankroll["realized_pnl"],
            "today_pnl": bankroll["today_pnl"],
            "is_paper": True,
            "predictions_logged": n_predictions,
        },
        "arm1_scanner": {
            "tracking": n_markets,
            "in_research": 0,
            "queued": 0,
        },
        "arm2_edges": [
            {
                "question": e["question"],
                "category": e["category"],
                "edge": round(e["edge"], 3),
                "p_yes": round(e["p_yes"], 3),
                "market_price": round(e["px"], 3),
            }
            for e in edges_rows
        ],
        "arm3_recent": [
            {
                "question": r["question"],
                "p_yes": round(r["p_yes"], 3),
                "confidence": round(r["confidence"], 2),
                "predicted_at": r["predicted_at"],
                "unciteable": bool(r["unciteable"]),
                "age_label": _age_label(r["predicted_at"], now),
            }
            for r in recent_rows
        ],
        "arm4_calibration": {
            "rows": [
                {
                    "category": c["category"],
                    "brier": round(c["brier"], 3),
                    "bias": round(c["bias"], 3),
                    "n": c["n"],
                }
                for c in calib_cat_rows
            ],
            "total_predictions": n_predictions,
            "phase0_message": _phase0_calibration_message(n_predictions),
        },
        "arm5_sources": {
            "calibrated": [
                {
                    "source": _short_domain(s["source_url"]),
                    "trust": round(s["brier_with"], 2),
                    "n": s["n_with"],
                }
                for s in src_calib_rows
            ],
            "raw_counts": [
                {"source": _short_domain(s["source_url"]), "n": s["n"]}
                for s in src_count_rows
            ],
        },
        "arm6_citation": {
            "rate": round(citeable_rate, 2),
            "citeable_total": cit["citeable"] or 0,
            "unciteable_total": cit["unciteable"] or 0,
            "buckets": cite_buckets,
        },
        "arm7_pending": {
            "open": n_unresolved,
            "next": (
                {
                    "question": next_pending["question"],
                    "category": next_pending["category"],
                    "resolves_at": next_pending["resolves_at"],
                    "time_to_resolve": _time_until(next_pending["resolves_at"], now),
                    "p_yes": round(next_pending["p_yes"], 2) if next_pending["p_yes"] is not None else None,
                    "market_price": round(next_pending["px"], 2) if next_pending["px"] is not None else None,
                }
                if next_pending
                else None
            ),
        },
        "arm8_resolutions": [
            {
                "question": r["question"],
                "outcome": r["outcome"],
                "p_yes": round(r["p_yes"], 2) if r["p_yes"] is not None else None,
                "hit": _hit(r["outcome"], r["p_yes"]),
                "resolved_at": r["resolved_at"],
            }
            for r in res_rows
        ],
        "freq_lever": freq_lever,
        "arm9_hype_fade": hype_detector.read_hud_data(),
        "arm10_copy_trade": _build_arm10(con),
    }


def _compute_bankroll(con: sqlite3.Connection) -> dict:
    """Returns dict with starting, open_exposure, realized_pnl, available."""
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(size_usd), 0.0) FROM trades WHERE paper=1 AND status='open'"
        ).fetchone()
        open_exposure = float(row[0]) if row else 0.0

        row2 = con.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0.0) FROM trades WHERE paper=1 AND status='closed'"
        ).fetchone()
        realized_pnl = float(row2[0]) if row2 else 0.0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row3 = con.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0.0) FROM trades "
            "WHERE paper=1 AND status='closed' AND closed_at >= ?",
            (today,),
        ).fetchone()
        today_pnl = float(row3[0]) if row3 else 0.0

        available = max(0.01, CONFIG.starting_bankroll_usd - open_exposure + realized_pnl)
        return {
            "starting": CONFIG.starting_bankroll_usd,
            "open_exposure": round(open_exposure, 4),
            "realized_pnl": round(realized_pnl, 4),
            "today_pnl": round(today_pnl, 4),
            "available": round(available, 4),
        }
    except Exception:
        return {
            "starting": CONFIG.starting_bankroll_usd,
            "open_exposure": 0.0,
            "realized_pnl": 0.0,
            "today_pnl": 0.0,
            "available": CONFIG.starting_bankroll_usd,
        }


def _read_freq_lever_state(bankroll_data: dict | float) -> dict:
    if isinstance(bankroll_data, dict):
        available = bankroll_data["available"]
        starting = bankroll_data["starting"]
        open_exposure = bankroll_data["open_exposure"]
        realized_pnl = bankroll_data["realized_pnl"]
    else:
        available = float(bankroll_data)
        starting = CONFIG.starting_bankroll_usd
        open_exposure = 0.0
        realized_pnl = 0.0

    def _label(name: str) -> str:
        return name.split("_", 1)[1] if "_" in name else name

    try:
        mode = "auto"
        manual_position = 3
        if FREQ_LEVER_PATH.exists():
            data = json.loads(FREQ_LEVER_PATH.read_text())
            mode = data.get("mode", "auto")
            manual_position = max(1, min(len(TIERS), int(data.get("manual_position", 3))))

        auto_tier = CONFIG.active_tier(available)
        auto_position = next((i + 1 for i, t in enumerate(TIERS) if t.name == auto_tier.name), 1)
        effective_position = manual_position if mode == "manual" else auto_position
        effective_tier = TIERS[effective_position - 1]
        override_warning = mode == "manual" and manual_position > auto_position

        return {
            "mode": mode,
            "manual_position": manual_position,
            "auto_position": auto_position,
            "effective_position": effective_position,
            "position_name": effective_tier.name,
            "position_label": _label(effective_tier.name),
            "bankroll": round(available, 2),
            "bankroll_starting": round(starting, 2),
            "bankroll_exposure": round(open_exposure, 4),
            "bankroll_realized": round(realized_pnl, 4),
            "effective_settings": {
                "max_stake_usd": effective_tier.max_stake_usd,
                "min_edge_to_trade": effective_tier.min_edge_to_trade,
                "max_edge_to_trade": effective_tier.max_edge_to_trade,
                "daily_loss_cap_usd": effective_tier.daily_loss_cap_usd,
                "kelly_fraction": effective_tier.kelly_fraction,
            },
            "all_positions": [
                {
                    "position": i + 1,
                    "name": t.name,
                    "label": _label(t.name),
                    "min_bankroll": t.min_bankroll,
                    "max_stake_usd": t.max_stake_usd,
                    "min_edge_to_trade": t.min_edge_to_trade,
                    "max_edge_to_trade": t.max_edge_to_trade,
                    "daily_loss_cap_usd": t.daily_loss_cap_usd,
                    "kelly_fraction": t.kelly_fraction,
                }
                for i, t in enumerate(TIERS)
            ],
            "override_warning": override_warning,
        }
    except Exception:
        return {
            "mode": "auto",
            "manual_position": 3,
            "auto_position": 3,
            "effective_position": 3,
            "position_name": "P03_Default",
            "position_label": "Default",
            "bankroll": round(available, 2),
            "bankroll_starting": round(starting, 2),
            "bankroll_exposure": round(open_exposure, 4),
            "bankroll_realized": round(realized_pnl, 4),
            "effective_settings": {},
            "all_positions": [],
            "override_warning": False,
        }


def _approx_cycle_count(con: sqlite3.Connection) -> int:
    try:
        row = con.execute(
            "SELECT COUNT(DISTINCT substr(snapshot_at, 1, 16)) FROM market_snapshots"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _seconds_to_next_scan(con: sqlite3.Connection) -> int | None:
    try:
        row = con.execute(
            "SELECT MAX(snapshot_at) FROM market_snapshots"
        ).fetchone()
        if not row or not row[0]:
            return None
        last = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        loop_interval = int(getattr(CONFIG, "loop_interval_seconds", 900))
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return max(0, int(loop_interval - elapsed))
    except Exception:
        return None


def _phase0_calibration_message(n_predictions: int) -> str:
    threshold = 50
    if n_predictions >= threshold:
        return f"Brier scoring active. {n_predictions} predictions logged."
    return f"Brier scoring activates at {threshold} resolutions. Currently: {n_predictions}."


def _short_domain(url: str | None) -> str:
    if not url:
        return "—"
    try:
        host = urlparse(url).hostname or url
        return host.replace("www.", "")
    except Exception:
        return url[:30]


def _age_label(iso: str | None, now: datetime) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h"
        return f"{s // 86400}d"
    except Exception:
        return ""


def _time_until(iso: str | None, now: datetime) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - now
        s = int(delta.total_seconds())
        if s < 0:
            return "overdue"
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h {(s % 3600) // 60}m"
        return f"{s // 86400}d {(s % 86400) // 3600}h"
    except Exception:
        return ""


def _hit(outcome: str | None, p_yes: float | None) -> bool | None:
    if outcome is None or p_yes is None:
        return None
    if outcome == "YES":
        return p_yes >= 0.5
    if outcome == "NO":
        return p_yes < 0.5
    return None


def _empty_state() -> dict[str, Any]:
    return {
        "tick": int(time.time()),
        "system": {
            "cycle": 0,
            "uptime_seconds": int(time.time() - _START_TIME),
            "phase": "phase 0",
            "scan_status": "idle",
            "research_status": "idle",
            "ledger_status": "no_db",
            "exec_status": "disabled",
            "next_scan_seconds": None,
            "loop_interval_seconds": int(getattr(CONFIG, "loop_interval_seconds", 900)),
            "predictions_today": 0,
        },
        "total": {
            "paper_pnl": 0.0,
            "today_pnl": 0.0,
            "is_paper": True,
            "predictions_logged": 0,
        },
        "arm1_scanner": {"tracking": 0, "in_research": 0, "queued": 0},
        "arm2_edges": [],
        "arm3_recent": [],
        "arm4_calibration": {"rows": [], "total_predictions": 0, "phase0_message": "No DB yet."},
        "arm5_sources": {"calibrated": [], "raw_counts": []},
        "arm6_citation": {"rate": 0.0, "citeable_total": 0, "unciteable_total": 0, "buckets": []},
        "arm7_pending": {"open": 0, "next": None},
        "arm8_resolutions": [],
        "freq_lever": {
            "mode": "auto",
            "manual_position": 3,
            "auto_position": 3,
            "effective_position": 3,
            "position_name": "P03_Default",
            "position_label": "Default",
            "bankroll": CONFIG.starting_bankroll_usd,
            "bankroll_starting": CONFIG.starting_bankroll_usd,
            "bankroll_exposure": 0.0,
            "bankroll_realized": 0.0,
            "effective_settings": {
                "max_stake_usd": 0.50,
                "min_edge_to_trade": 0.10,
                "max_edge_to_trade": 0.20,
                "daily_loss_cap_usd": 2.00,
                "kelly_fraction": 0.25,
            },
            "all_positions": [],
            "override_warning": False,
        },
        "arm9_hype_fade": {"enabled": False},
        "arm10_copy_trade": {"enabled": False, "active_wallets": [], "last_events": []},
    }


def _build_arm10(con: sqlite3.Connection) -> dict:
    """Build ARM 10 copy-trade panel data."""
    hud = copy_trader.read_hud_data()
    # Enrich with live copy-trade P&L from DB
    try:
        today = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_usd), 0) FROM trades "
            "WHERE copy_source IS NOT NULL AND paper=1 AND entered_at >= ?",
            (today,),
        ).fetchone()
        hud["copies_today"] = int(row[0]) if row else 0
        hud["daily_exposure"] = round(float(row[1]), 4) if row else 0.0
    except Exception:
        hud["copies_today"] = 0
        hud["daily_exposure"] = 0.0
    return hud


# ── HTTP handler ──────────────────────────────────────────────────────────────


class HUDHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _serve_static(self, path: Path, mime: str) -> None:
        if not path.exists():
            self.send_error(404, f"Not found: {path.name}")
            return
        try:
            content = path.read_bytes()
        except Exception as exc:
            self.send_error(500, str(exc))
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                state = _read_state()
                payload = f"data: {json.dumps(state)}\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_static(HUD_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._serve_json(_read_state())
            except Exception as exc:
                log.error("hud.state_failed", error=str(exc))
                self.send_error(500, str(exc))
        elif path == "/sse/state":
            try:
                self._serve_sse()
            except Exception as exc:
                log.error("hud.sse_failed", error=str(exc))
        elif path == "/freq_lever":
            try:
                con = _ro_connect()
                bankroll = _compute_bankroll(con) if con else {
                    "starting": CONFIG.starting_bankroll_usd,
                    "open_exposure": 0.0,
                    "realized_pnl": 0.0,
                    "today_pnl": 0.0,
                    "available": CONFIG.starting_bankroll_usd,
                }
                if con:
                    con.close()
                self._serve_json(_read_freq_lever_state(bankroll))
            except Exception as exc:
                self.send_error(500, str(exc))
        else:
            self._serve_hud_static(path)

    def _serve_hud_static(self, req_path: str) -> None:
        # Resolve to an absolute path inside HUD_DIR — reject any traversal attempt.
        try:
            rel = req_path.lstrip("/")
            target = (HUD_DIR / rel).resolve()
            if not str(target).startswith(str(HUD_DIR.resolve())):
                self.send_error(403)
                return
        except Exception:
            self.send_error(400)
            return
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(str(target))
        self._serve_static(target, mime or "application/octet-stream")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/freq_lever":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))

                mode = data.get("mode", "auto")
                if mode not in ("auto", "manual"):
                    mode = "auto"
                manual_position = max(1, min(len(TIERS), int(data.get("manual_position", 3))))

                FREQ_LEVER_PATH.write_text(
                    json.dumps(
                        {
                            "mode": mode,
                            "manual_position": manual_position,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        indent=2,
                    )
                )
                log.info("hud.freq_lever_saved", mode=mode, manual_position=manual_position)
                self._serve_json({"ok": True, "mode": mode, "manual_position": manual_position})
            except Exception as exc:
                log.error("hud.freq_lever_save_failed", error=str(exc))
                self.send_error(400, str(exc))
        else:
            self.send_error(404)


# ── Entry point ───────────────────────────────────────────────────────────────


def serve(host: str = HUD_HOST, port: int = HUD_PORT) -> None:
    server = ThreadingHTTPServer((host, port), HUDHandler)
    log.info("hud.start", host=host, port=port, db=str(CONFIG.db_path))
    print(f"\n  Octogon HUD: http://{host}:{port}/\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("hud.stop")
        server.server_close()


if __name__ == "__main__":
    serve()
