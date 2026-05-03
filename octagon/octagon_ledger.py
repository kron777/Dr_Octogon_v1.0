"""
SQLite ledger — predictions, market snapshots, resolutions, calibration tables.

WAL mode + gatekeeper enforced: importing this module installs the gatekeeper globally
(the gatekeeper module calls install() at import time). The ledger then calls
set_checkpoint_db() so the watchdog knows which file to checkpoint.

Reasoning traces live on disk under TRACE_DIR; the DB row holds the path only.
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
from octagon.octagon_models import MarketSnapshot, Prediction, Resolution, SourceDoc

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
    base_rate_ref_class        TEXT,
    base_rate_source           TEXT,
    adjustments                TEXT,   -- JSON
    edge_cases_considered      TEXT,   -- JSON
    predicted_at               TEXT NOT NULL,
    ttl_seconds                INTEGER NOT NULL,
    trace_path                 TEXT
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
        trace_path = self.trace_dir / f"{prediction.prediction_id}.json"
        trace_path.write_text(
            json.dumps(
                {
                    "prediction_id": prediction.prediction_id,
                    "market_id": prediction.market_id,
                    "reasoning": prediction.reasoning,
                    "base_rate": prediction.base_rate,
                    "base_rate_ref_class": prediction.base_rate_ref_class,
                    "base_rate_source": prediction.base_rate_source,
                    "adjustments": prediction.adjustments,
                    "edge_cases_considered": prediction.edge_cases_considered,
                    "predicted_at": prediction.predicted_at.isoformat(),
                },
                indent=2,
            )
        )

        con = self._connect()
        con.execute(
            """
            INSERT OR REPLACE INTO predictions
                (prediction_id, market_id, p_yes, p_yes_raw, confidence, edge,
                 market_price_at_prediction, resolution_clarity, unciteable,
                 base_rate, base_rate_ref_class, base_rate_source,
                 adjustments, edge_cases_considered,
                 predicted_at, ttl_seconds, trace_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                prediction.base_rate_ref_class,
                prediction.base_rate_source,
                json.dumps(prediction.adjustments),
                json.dumps(prediction.edge_cases_considered),
                prediction.predicted_at.isoformat(),
                prediction.ttl_seconds,
                str(trace_path),
            ),
        )

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
                   base_rate, base_rate_ref_class, base_rate_source,
                   adjustments, edge_cases_considered,
                   predicted_at, ttl_seconds
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
                base_rate, ref_class, base_src, adj_json, ec_json,
                predicted_at, ttl,
            ) = row
            if mid not in result:
                result[mid] = Prediction(
                    prediction_id=pid,
                    market_id=mid,
                    p_yes=p_yes,
                    p_yes_raw=p_yes_raw,
                    confidence=conf,
                    edge=edge,
                    reasoning="",
                    evidence_refs=[],
                    market_price_at_prediction=price_at,
                    resolution_clarity=clarity,
                    unciteable=bool(unciteable),
                    predicted_at=datetime.fromisoformat(predicted_at),
                    ttl_seconds=ttl,
                    base_rate=base_rate or 0.5,
                    base_rate_ref_class=ref_class or "",
                    base_rate_source=base_src or "",
                    adjustments=json.loads(adj_json or "[]"),
                    edge_cases_considered=json.loads(ec_json or "[]"),
                )
        return result

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
