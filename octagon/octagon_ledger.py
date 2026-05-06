"""
SQLite ledger — predictions, market snapshots, resolutions, calibration tables.

WAL mode + gatekeeper enforced: importing this module installs the gatekeeper globally
(the gatekeeper module calls install() at import time). The ledger then calls
set_checkpoint_db() so the watchdog knows which file to checkpoint.

Reasoning traces live on disk (written by octagon_research); the DB row holds the path only.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import structlog

import octagon.octagon_db_gatekeeper as _gk  # installs gatekeeper on import
from octagon.octagon_config import CONFIG
from octagon.octagon_models import Adjustment, MarketSnapshot, Prediction, Resolution, SourceDoc

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id          TEXT PRIMARY KEY,
    question           TEXT NOT NULL,
    category           TEXT,
    resolution_criteria TEXT,
    resolves_at        TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id    TEXT PRIMARY KEY,
    market_id      TEXT NOT NULL REFERENCES markets(market_id),
    yes_price      REAL,
    bid_yes        REAL,
    ask_yes        REAL,
    depth_yes_usd  REAL,
    depth_no_usd   REAL,
    volume_24h     REAL,
    snapshot_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id              TEXT PRIMARY KEY,
    market_id                  TEXT NOT NULL REFERENCES markets(market_id),
    p_yes                      REAL NOT NULL,
    p_yes_raw                  REAL NOT NULL,
    confidence                 REAL NOT NULL,
    edge                       REAL NOT NULL,
    market_price_at_prediction REAL NOT NULL,
    resolution_clarity         REAL NOT NULL,
    unciteable                 INTEGER NOT NULL DEFAULT 0,
    base_rate                  REAL,
    base_rate_reference_class  TEXT,
    edge_cases                 TEXT,   -- JSON list of strings
    predicted_at               TEXT NOT NULL,
    ttl_seconds                INTEGER NOT NULL,
    reasoning_trace_path       TEXT,
    model_used                 TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id  TEXT PRIMARY KEY,
    prediction_id  TEXT NOT NULL REFERENCES predictions(prediction_id),
    direction      TEXT NOT NULL,
    magnitude_pp   REAL NOT NULL,
    source_url     TEXT NOT NULL,
    rationale      TEXT,
    ord            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS evidence_refs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id  TEXT NOT NULL REFERENCES predictions(prediction_id),
    source_url     TEXT NOT NULL,
    source_class   TEXT,
    fetched_at     TEXT
);

CREATE TABLE IF NOT EXISTS resolutions (
    market_id    TEXT PRIMARY KEY REFERENCES markets(market_id),
    outcome      TEXT NOT NULL,
    resolved_at  TEXT NOT NULL,
    source       TEXT
);

CREATE TABLE IF NOT EXISTS calibration_category (
    category              TEXT PRIMARY KEY,
    brier                 REAL,
    bias                  REAL,
    n                     INTEGER DEFAULT 0,
    last_recalibrated_at  TEXT
);

CREATE TABLE IF NOT EXISTS calibration_source (
    source_url            TEXT PRIMARY KEY,
    brier_with            REAL,
    brier_without         REAL,
    n_with                INTEGER DEFAULT 0,
    n_without             INTEGER DEFAULT 0,
    last_recalibrated_at  TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id       TEXT PRIMARY KEY,
    prediction_id  TEXT NOT NULL REFERENCES predictions(prediction_id),
    market_id      TEXT NOT NULL REFERENCES markets(market_id),
    side           TEXT NOT NULL,     -- 'YES' or 'NO'
    entry_price    REAL NOT NULL,
    size_usd       REAL NOT NULL,
    entered_at     TEXT NOT NULL,
    paper          INTEGER NOT NULL DEFAULT 1,  -- 1=paper, 0=live
    status         TEXT NOT NULL DEFAULT 'open' -- 'open', 'closed', 'cancelled'
);
"""


class OctagonLedger:
    def __init__(self, db_path: str = CONFIG.db_path):
        self.db_path = os.path.expanduser(db_path)
        self.trace_dir = Path(os.path.expanduser(CONFIG.trace_dir))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        _gk.set_checkpoint_db(self.db_path)
        self._install_schema()
        log.info("ledger.ready", db=self.db_path, trace_dir=str(self.trace_dir))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _install_schema(self) -> None:
        con = self._connect()
        con.executescript(_SCHEMA)
        for stmt in [
            "ALTER TABLE predictions ADD COLUMN model_used TEXT DEFAULT ''",
            "ALTER TABLE trades ADD COLUMN pnl_usd REAL DEFAULT NULL",
            "ALTER TABLE trades ADD COLUMN closed_at TEXT DEFAULT NULL",
            "ALTER TABLE trades ADD COLUMN copy_source TEXT DEFAULT NULL",
        ]:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        con.commit()
        con.close()

    # ── Writes ────────────────────────────────────────────────────────────────

    def log_market_snapshot(self, market: MarketSnapshot) -> None:
        snapshot_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        con = self._connect()
        con.execute(
            """
            INSERT INTO markets
                (market_id, question, category, resolution_criteria,
                 resolves_at, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (
                market.market_id,
                market.question,
                market.category,
                market.resolution_criteria,
                market.resolves_at.isoformat() if market.resolves_at else None,
                now,
                now,
            ),
        )
        con.execute(
            """
            INSERT INTO market_snapshots
                (snapshot_id, market_id, yes_price, bid_yes, ask_yes,
                 depth_yes_usd, depth_no_usd, volume_24h, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                market.market_id,
                market.yes_price,
                market.bid_yes,
                market.ask_yes,
                market.depth_yes_usd,
                market.depth_no_usd,
                market.volume_24h,
                market.snapshot_at.isoformat(),
            ),
        )
        con.commit()
        con.close()

    def log_prediction(
        self, prediction: Prediction, source_docs: list[SourceDoc] | None = None
    ) -> None:
        con = self._connect()
        con.execute(
            """
            INSERT OR REPLACE INTO predictions
                (prediction_id, market_id, p_yes, p_yes_raw, confidence, edge,
                 market_price_at_prediction, resolution_clarity, unciteable,
                 base_rate, base_rate_reference_class, edge_cases,
                 predicted_at, ttl_seconds, reasoning_trace_path, model_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction.prediction_id,
                prediction.market_id,
                prediction.p_yes,
                prediction.p_yes_raw,
                prediction.confidence,
                prediction.edge,
                prediction.market_price_at_prediction,
                prediction.resolution_clarity,
                int(prediction.unciteable),
                prediction.base_rate,
                prediction.base_rate_reference_class,
                json.dumps(prediction.edge_cases),
                prediction.predicted_at.isoformat(),
                prediction.ttl_seconds,
                prediction.reasoning_trace_path,
                prediction.model_used,
            ),
        )

        for i, adj in enumerate(prediction.adjustments):
            con.execute(
                """
                INSERT INTO adjustments
                    (adjustment_id, prediction_id, direction, magnitude_pp, source_url, rationale, ord)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    prediction.prediction_id,
                    adj.direction,
                    adj.magnitude_pp,
                    adj.source_url,
                    adj.rationale,
                    i,
                ),
            )

        # Populate evidence_refs: prefer full SourceDoc objects; fall back to URL strings
        if source_docs:
            for doc in source_docs:
                con.execute(
                    """
                    INSERT INTO evidence_refs
                        (prediction_id, source_url, source_class, fetched_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        prediction.prediction_id,
                        doc.url,
                        doc.source_class,
                        doc.fetched_at.isoformat(),
                    ),
                )
        else:
            for url in prediction.evidence_refs:
                con.execute(
                    "INSERT INTO evidence_refs (prediction_id, source_url) VALUES (?, ?)",
                    (prediction.prediction_id, url),
                )

        con.commit()
        con.close()
        log.info(
            "ledger.prediction_logged",
            prediction_id=prediction.prediction_id,
            p_yes=round(prediction.p_yes, 3),
            confidence=round(prediction.confidence, 3),
            unciteable=prediction.unciteable,
        )

    def log_resolution(self, resolution: Resolution) -> None:
        con = self._connect()
        con.execute(
            """
            INSERT OR REPLACE INTO resolutions
                (market_id, outcome, resolved_at, source)
            VALUES (?, ?, ?, ?)
            """,
            (
                resolution.market_id,
                resolution.outcome,
                resolution.resolved_at.isoformat(),
                resolution.source,
            ),
        )
        con.commit()
        con.close()
        log.info(
            "ledger.resolution_logged",
            market_id=resolution.market_id,
            outcome=resolution.outcome,
        )

    # ── Reads ─────────────────────────────────────────────────────────────────

    def recent_predictions(self, window_seconds: int | None = None) -> dict[str, Prediction]:
        """Most recent prediction per market within window_seconds. Used by triage."""
        if window_seconds is None:
            window_seconds = CONFIG.ttl_seconds * 2
        cutoff = (datetime.utcnow() - timedelta(seconds=window_seconds)).isoformat()
        con = self._connect()
        cur = con.cursor()
        cur.execute(
            """
            SELECT prediction_id, market_id, p_yes, p_yes_raw, confidence, edge,
                   market_price_at_prediction, resolution_clarity, unciteable,
                   base_rate, base_rate_reference_class, edge_cases,
                   predicted_at, ttl_seconds, reasoning_trace_path, model_used
            FROM predictions
            WHERE predicted_at >= ?
            ORDER BY predicted_at DESC
            """,
            (cutoff,),
        )
        rows = cur.fetchall()
        con.close()

        result: dict[str, Prediction] = {}
        for row in rows:
            (
                pid, mid, p_yes, p_yes_raw, conf, edge, price_at, clarity, unciteable,
                base_rate, ref_class, ec_json,
                predicted_at, ttl, trace_path, model_used,
            ) = row
            if mid not in result:
                result[mid] = Prediction(
                    prediction_id=pid,
                    market_id=mid,
                    p_yes=p_yes,
                    p_yes_raw=p_yes_raw,
                    confidence=conf,
                    edge=edge,
                    market_price_at_prediction=price_at,
                    resolution_clarity=clarity,
                    unciteable=bool(unciteable),
                    base_rate=base_rate or 0.5,
                    base_rate_reference_class=ref_class or "",
                    adjustments=[],
                    edge_cases=json.loads(ec_json or "[]"),
                    reasoning_trace_path=trace_path or "",
                    evidence_refs=[],
                    predicted_at=datetime.fromisoformat(predicted_at),
                    ttl_seconds=ttl,
                    model_used=model_used or "",
                )
        return result

    def log_trade(self, trade: "Trade", paper: bool = True) -> None:
        con = self._connect()
        con.execute(
            """
            INSERT INTO trades
                (trade_id, prediction_id, market_id, side,
                 entry_price, size_usd, entered_at, paper, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                trade.trade_id,
                trade.prediction_id,
                trade.market_id,
                trade.side,
                trade.entry_price,
                trade.size_usd,
                trade.entered_at.isoformat(),
                int(paper),
            ),
        )
        con.commit()
        con.close()
        log.info(
            "ledger.trade_logged",
            trade_id=trade.trade_id,
            market_id=trade.market_id,
            side=trade.side,
            size_usd=round(trade.size_usd, 4),
            entry_price=round(trade.entry_price, 4),
            paper=paper,
        )

    def log_copy_trade(self, trade: "Trade", copy_source: str, paper: bool = True) -> None:
        """Like log_trade but sets copy_source column for P&L attribution."""
        con = self._connect()
        con.execute(
            """
            INSERT INTO trades
                (trade_id, prediction_id, market_id, side,
                 entry_price, size_usd, entered_at, paper, status, copy_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                trade.trade_id,
                trade.prediction_id,
                trade.market_id,
                trade.side,
                trade.entry_price,
                trade.size_usd,
                trade.entered_at.isoformat(),
                int(paper),
                copy_source,
            ),
        )
        con.commit()
        con.close()
        log.info(
            "ledger.copy_trade_logged",
            trade_id=trade.trade_id,
            market_id=trade.market_id,
            side=trade.side,
            size_usd=round(trade.size_usd, 4),
            entry_price=round(trade.entry_price, 4),
            copy_source=copy_source,
            paper=paper,
        )

    def copy_trade_daily_loss_usd(self) -> float:
        """Sum of copy-trade stakes entered today (for daily loss cap enforcement)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        con = self._connect()
        cur = con.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(size_usd), 0.0)
            FROM trades
            WHERE paper = 1
              AND copy_source IS NOT NULL
              AND entered_at >= ?
            """,
            (today,),
        )
        result = cur.fetchone()[0]
        con.close()
        return float(result)

    def copy_trades_for_hud(self, limit: int = 5) -> list[dict]:
        """Return last N copy trades for the HUD ARM 10 panel."""
        con = self._connect()
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            """
            SELECT t.trade_id, t.market_id, t.side, t.size_usd, t.entry_price,
                   t.entered_at, t.status, t.pnl_usd, t.copy_source,
                   m.question
            FROM trades t
            LEFT JOIN markets m ON m.market_id = t.market_id
            WHERE t.copy_source IS NOT NULL
            ORDER BY t.entered_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return rows

    def today_paper_loss_usd(self) -> float:
        """Sum of paper trade stakes entered today (worst-case daily exposure)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        con = self._connect()
        cur = con.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(size_usd), 0.0)
            FROM trades
            WHERE paper = 1
              AND entered_at >= ?
            """,
            (today,),
        )
        result = cur.fetchone()[0]
        con.close()
        return float(result)

    def total_trades(self) -> int:
        """Total number of trades ever logged (paper + live)."""
        con = self._connect()
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM trades")
        result = cur.fetchone()[0]
        con.close()
        return int(result)

    def get_open_trades_for_market(self, market_id: str) -> list:
        """Return all open paper trades for a market."""
        from octagon.octagon_models import Trade
        con = self._connect()
        cur = con.cursor()
        cur.execute(
            "SELECT trade_id, prediction_id, market_id, side, entry_price, size_usd, entered_at "
            "FROM trades WHERE market_id = ? AND status = 'open' AND paper = 1",
            (market_id,),
        )
        rows = cur.fetchall()
        con.close()
        result = []
        for r in rows:
            result.append(Trade(
                trade_id=r[0],
                prediction_id=r[1],
                market_id=r[2],
                side=r[3],
                entry_price=r[4],
                size_usd=r[5],
                entered_at=datetime.fromisoformat(r[6]),
            ))
        return result

    def close_trade(self, trade_id: str, pnl: float, closed_at: datetime) -> None:
        con = self._connect()
        con.execute(
            "UPDATE trades SET status='closed', pnl_usd=?, closed_at=? WHERE trade_id=?",
            (pnl, closed_at.isoformat(), trade_id),
        )
        con.commit()
        con.close()
        log.info("ledger.trade_closed", trade_id=trade_id, pnl_usd=round(pnl, 4))

    def current_bankroll(self) -> float:
        """
        Bankroll = STARTING_BANKROLL_USD - open paper exposure + realized paper P&L.
        Open exposure prevents over-allocation before settlement.
        Realized P&L adjusts permanently after each market resolves.
        """
        try:
            con = self._connect()
            cur = con.cursor()
            cur.execute(
                "SELECT COALESCE(SUM(size_usd), 0.0) FROM trades WHERE paper=1 AND status='open'"
            )
            open_exposure = float(cur.fetchone()[0])
            cur.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0.0) FROM trades WHERE paper=1 AND status='closed'"
            )
            realized_pnl = float(cur.fetchone()[0])
            con.close()
            return max(0.01, CONFIG.starting_bankroll_usd - open_exposure + realized_pnl)
        except Exception:
            return CONFIG.starting_bankroll_usd

    def realized_pnl_today(self) -> float:
        """Sum of pnl_usd from paper trades closed today (UTC)."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            con = self._connect()
            cur = con.cursor()
            cur.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0.0) FROM trades "
                "WHERE paper=1 AND status='closed' AND closed_at >= ?",
                (today,),
            )
            result = float(cur.fetchone()[0])
            con.close()
            return result
        except Exception:
            return 0.0

    def unresolved_markets_past_resolution_time(self) -> list[str]:
        """Market IDs that have passed resolves_at but have no resolution record."""
        now = datetime.utcnow().isoformat()
        con = self._connect()
        cur = con.cursor()
        cur.execute(
            """
            SELECT m.market_id FROM markets m
            LEFT JOIN resolutions r ON m.market_id = r.market_id
            WHERE r.market_id IS NULL
              AND m.resolves_at IS NOT NULL
              AND m.resolves_at < ?
            """,
            (now,),
        )
        rows = cur.fetchall()
        con.close()
        return [row[0] for row in rows]

    def invalid_resolution_market_ids(self) -> list[str]:
        """Market IDs whose stored resolution is INVALID — eligible for re-check."""
        con = self._connect()
        cur = con.cursor()
        cur.execute(
            "SELECT market_id FROM resolutions WHERE outcome = 'INVALID'"
        )
        rows = cur.fetchall()
        con.close()
        return [row[0] for row in rows]

    def price_change_24h(self, market_id: str) -> float:
        """Absolute yes_price change over the last 24h from market_snapshots. 0.0 if no history."""
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        con = self._connect()
        cur = con.cursor()
        cur.execute(
            """
            SELECT MIN(yes_price), MAX(yes_price)
            FROM market_snapshots
            WHERE market_id = ? AND snapshot_at >= ?
            """,
            (market_id, cutoff),
        )
        row = cur.fetchone()
        con.close()
        if row and row[0] is not None and row[1] is not None:
            return abs(row[1] - row[0])
        return 0.0

    def summary(self) -> dict:
        """Aggregate stats for the report CLI."""
        con = self._connect()
        cur = con.cursor()
        stats: dict = {}

        cur.execute("SELECT COUNT(*) FROM markets")
        stats["total_markets"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM predictions")
        stats["total_predictions"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM resolutions")
        stats["total_resolutions"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM predictions WHERE unciteable = 1")
        stats["unciteable"] = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM predictions WHERE predicted_at > datetime('now','-24 hours')"
        )
        stats["predictions_24h"] = cur.fetchone()[0]

        cur.execute("SELECT category, COUNT(*) FROM markets GROUP BY category ORDER BY 2 DESC")
        stats["by_category"] = dict(cur.fetchall())

        cur.execute(
            """
            SELECT m.market_id, m.question, m.resolves_at
            FROM markets m
            LEFT JOIN resolutions r ON m.market_id = r.market_id
            WHERE r.market_id IS NULL
            ORDER BY m.resolves_at ASC
            LIMIT 5
            """
        )
        stats["oldest_unresolved"] = cur.fetchall()

        cur.execute(
            """
            SELECT AVG(confidence), AVG(resolution_clarity), AVG(ABS(edge))
            FROM predictions
            WHERE predicted_at > datetime('now','-7 days')
            """
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            stats["avg_confidence_7d"] = round(row[0], 3)
            stats["avg_clarity_7d"] = round(row[1], 3)
            stats["avg_abs_edge_7d"] = round(row[2], 3)

        con.close()
        return stats
