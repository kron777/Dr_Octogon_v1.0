"""Unit tests for octagon_triage.filter() — one test per drop reason."""

from datetime import datetime, timedelta
import pytest

from octagon.octagon_models import Adjustment, MarketSnapshot, Prediction
from octagon.octagon_triage import filter as triage_filter


def _market(**overrides) -> MarketSnapshot:
    defaults = dict(
        market_id="m001",
        question="Will X happen?",
        yes_price=0.50,
        bid_yes=0.49,
        ask_yes=0.51,  # spread=0.02, clearly below max_spread=0.04
        depth_yes_usd=1000.0,
        depth_no_usd=1000.0,
        volume_24h=5000.0,
        resolves_at=datetime.utcnow() + timedelta(hours=48),
        resolution_criteria="Resolves YES if X.",
        category="Politics",
        snapshot_at=datetime.utcnow(),
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


def _prediction(market_id: str, price: float = 0.50, age_seconds: int = 0) -> Prediction:
    return Prediction(
        prediction_id="p001",
        market_id=market_id,
        p_yes=0.55,
        p_yes_raw=0.55,
        confidence=0.7,
        edge=0.05,
        reasoning_trace_path="",
        evidence_refs=[],
        market_price_at_prediction=price,
        resolution_clarity=0.8,
        unciteable=False,
        predicted_at=datetime.utcnow() - timedelta(seconds=age_seconds),
        ttl_seconds=3600,
        base_rate=0.5,
        base_rate_reference_class="",
        adjustments=[],
        edge_cases=[],
    )


# ── Drop reasons ─────────────────────────────────────────────────────────────

def test_passes_when_valid():
    markets = [_market()]
    result = triage_filter(markets, {})
    assert len(result) == 1


def test_skip_within_ttl_no_price_movement():
    """Market already evaluated within TTL with negligible price change → skip."""
    m = _market(market_id="m001", yes_price=0.50)
    pred = _prediction("m001", price=0.50, age_seconds=100)  # age < ttl=3600
    result = triage_filter([m], {"m001": pred})
    assert result == []


def test_repredicts_when_price_moved():
    """Same market but price has moved past threshold → should pass through."""
    m = _market(market_id="m001", yes_price=0.60)
    pred = _prediction("m001", price=0.50, age_seconds=100)  # 0.10 > repredict_threshold=0.03
    result = triage_filter([m], {"m001": pred})
    assert len(result) == 1


def test_repredicts_when_ttl_expired():
    """TTL expired → should pass through even if price didn't move."""
    m = _market(market_id="m001", yes_price=0.50)
    pred = _prediction("m001", price=0.50, age_seconds=4000)  # age > ttl=3600
    result = triage_filter([m], {"m001": pred})
    assert len(result) == 1


def test_skip_depth_yes_too_low():
    m = _market(depth_yes_usd=100.0)  # below min_depth_usd=500
    result = triage_filter([m], {})
    assert result == []


def test_skip_depth_no_too_low():
    m = _market(depth_no_usd=100.0)
    result = triage_filter([m], {})
    assert result == []


def test_skip_spread_too_wide():
    m = _market(bid_yes=0.40, ask_yes=0.50)  # spread=0.10 > max_spread=0.04
    result = triage_filter([m], {})
    assert result == []


def test_skip_resolves_too_soon():
    m = _market(resolves_at=datetime.utcnow() + timedelta(hours=1))  # < min=4h
    result = triage_filter([m], {})
    assert result == []


def test_skip_resolves_too_far():
    m = _market(resolves_at=datetime.utcnow() + timedelta(days=20))  # > max=14d
    result = triage_filter([m], {})
    assert result == []


def test_skip_category_not_whitelisted():
    m = _market(category="Sports")
    result = triage_filter([m], {})
    assert result == []


def test_category_match_case_insensitive():
    """Category matching is case-insensitive."""
    m = _market(category="politics")  # lowercase
    result = triage_filter([m], {})
    assert len(result) == 1


def test_multiple_markets_mixed():
    """Valid market passes; invalid one doesn't."""
    good = _market(market_id="good")
    bad = _market(market_id="bad", category="Sports")
    result = triage_filter([good, bad], {})
    assert len(result) == 1
    assert result[0].market_id == "good"
