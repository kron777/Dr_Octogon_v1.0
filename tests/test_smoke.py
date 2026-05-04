"""
Smoke test: python -m octagon.main --once exits 0 against a fixture market list.

The scanner and research.evaluate are patched so no real API calls are made.
Verifies the full scan→triage→research→ledger pipeline runs without error
and that the DB is populated after one cycle.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from octagon.octagon_models import Adjustment, MarketSnapshot, Prediction
from octagon.octagon_ledger import OctagonLedger
from octagon.octagon_main import run_cycle


def _fixture_market(market_id: str = "smoke_mkt001") -> MarketSnapshot:
    return MarketSnapshot(
        market_id=market_id,
        question="Will the Federal Reserve raise rates at its June 2026 meeting?",
        yes_price=0.62,
        bid_yes=0.61,
        ask_yes=0.63,   # spread=0.02, clearly below max_spread=0.04
        depth_yes_usd=8000.0,
        depth_no_usd=7500.0,
        volume_24h=120000.0,
        resolves_at=datetime.utcnow() + timedelta(days=7),
        resolution_criteria=(
            "Resolves YES if the FOMC announces a target rate increase at its "
            "June 2026 meeting. Resolves NO otherwise."
        ),
        category="Macro",
        snapshot_at=datetime.utcnow(),
    )


def _fixture_prediction(
    market_id: str = "smoke_mkt001",
    trace_path: str = "/tmp/trace_smoke.json",
) -> Prediction:
    return Prediction(
        prediction_id=Prediction.new_id(),
        market_id=market_id,
        p_yes=0.68,
        p_yes_raw=0.67,
        confidence=0.72,
        edge=0.06,
        reasoning_trace_path=trace_path,
        evidence_refs=["https://federalreserve.gov/newsevents"],
        market_price_at_prediction=0.62,
        resolution_clarity=0.90,
        unciteable=False,
        predicted_at=datetime.utcnow(),
        ttl_seconds=3600,
        base_rate=0.68,
        base_rate_reference_class="Fed meetings following prior hike with inflation >2.5%",
        adjustments=[],
        edge_cases=[],
    )


@pytest.fixture
def tmp_ledger(tmp_path):
    db_path = str(tmp_path / "smoke_test.db")
    trace_dir = str(tmp_path / "traces")

    import octagon.octagon_config as cfg_mod
    cfg_mod.CONFIG.db_path = db_path
    cfg_mod.CONFIG.trace_dir = trace_dir

    return OctagonLedger(db_path=db_path)


def test_run_cycle_once(tmp_ledger):
    """Single cycle populates markets and predictions tables."""
    market = _fixture_market()
    prediction = _fixture_prediction()

    with (
        patch("octagon.octagon_scanner.scan", new=AsyncMock(return_value=[market])),
        patch("octagon.octagon_research.evaluate", new=AsyncMock(return_value=prediction)),
    ):
        asyncio.run(run_cycle(tmp_ledger))

    con = sqlite3.connect(tmp_ledger.db_path)
    markets_count = con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    predictions_count = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    con.close()

    assert markets_count >= 1, "markets table should have rows after one cycle"
    assert predictions_count >= 1, "predictions table should have rows after one cycle"


def test_trace_path_stored_after_cycle(tmp_ledger, tmp_path):
    """reasoning_trace_path from research.evaluate is stored in DB and points to a valid file."""
    import json

    market = _fixture_market()

    # Simulate what research.evaluate writes: a trace JSON on disk
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir(exist_ok=True)

    prediction_id = Prediction.new_id()
    trace_file = traces_dir / f"{prediction_id}.json"
    trace_data = {
        "prediction_id": prediction_id,
        "question": market.question,
        "forecast": {"p_yes": 0.68},
    }
    trace_file.write_text(json.dumps(trace_data, indent=2))

    prediction = _fixture_prediction(trace_path=str(trace_file))
    # Match the prediction_id to the trace file name
    from dataclasses import replace
    prediction = replace(prediction, prediction_id=prediction_id)

    with (
        patch("octagon.octagon_scanner.scan", new=AsyncMock(return_value=[market])),
        patch("octagon.octagon_research.evaluate", new=AsyncMock(return_value=prediction)),
    ):
        asyncio.run(run_cycle(tmp_ledger))

    # Verify DB stored the trace path
    con = sqlite3.connect(tmp_ledger.db_path)
    stored_path = con.execute(
        "SELECT reasoning_trace_path FROM predictions WHERE prediction_id = ?",
        (prediction_id,),
    ).fetchone()[0]
    con.close()

    assert stored_path == str(trace_file)
    assert trace_file.exists()
    data = json.loads(trace_file.read_text())
    assert data["prediction_id"] == prediction_id
