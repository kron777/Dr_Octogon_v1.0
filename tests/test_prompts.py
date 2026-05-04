"""
Schema-validation tests for octagon_prompts.

These tests use fixture JSON strings — no live API calls.
Each test validates that parse_*_response handles the documented schema correctly:
correct fields, clamping, arithmetic verification, and graceful error cases.
"""

import json
import pytest

from octagon.octagon_prompts import (
    format_criteria_prompt,
    format_forecaster_prompt,
    parse_criteria_response,
    parse_forecaster_response,
)
from octagon.octagon_models import SourceDoc
from datetime import datetime


# ── CRITERIA_PARSER fixtures ──────────────────────────────────────────────────

_CRITERIA_VALID = {
    "resolution_clarity": 0.8,
    "edge_cases": ["FOMC meeting cancelled or postponed"],
    "external_dependencies": ["Federal Reserve official statement"],
    "clarity_reasoning": "Clear resolution criteria with named authoritative source.",
}

_CRITERIA_MINIMAL = {
    "resolution_clarity": 0.6,
    "edge_cases": [],
    "external_dependencies": [],
    "clarity_reasoning": "",
}


def test_parse_criteria_valid():
    data = parse_criteria_response(json.dumps(_CRITERIA_VALID))
    assert abs(data["resolution_clarity"] - 0.8) < 0.001
    assert data["edge_cases"] == ["FOMC meeting cancelled or postponed"]
    assert data["external_dependencies"] == ["Federal Reserve official statement"]
    assert "clarity_reasoning" in data


def test_parse_criteria_minimal():
    data = parse_criteria_response(json.dumps(_CRITERIA_MINIMAL))
    assert data["edge_cases"] == []
    assert data["external_dependencies"] == []


def test_parse_criteria_clamps_clarity():
    over = {"resolution_clarity": 1.5, "edge_cases": [], "external_dependencies": [], "clarity_reasoning": ""}
    data = parse_criteria_response(json.dumps(over))
    assert data["resolution_clarity"] == 1.0

    under = {"resolution_clarity": -0.3, "edge_cases": [], "external_dependencies": [], "clarity_reasoning": ""}
    data = parse_criteria_response(json.dumps(under))
    assert data["resolution_clarity"] == 0.0


def test_parse_criteria_sets_defaults():
    minimal = {"resolution_clarity": 0.7}
    data = parse_criteria_response(json.dumps(minimal))
    assert data["edge_cases"] == []
    assert data["external_dependencies"] == []
    assert data["clarity_reasoning"] == ""


def test_parse_criteria_strips_json_fences():
    fenced = "```json\n" + json.dumps(_CRITERIA_VALID) + "\n```"
    data = parse_criteria_response(fenced)
    assert abs(data["resolution_clarity"] - 0.8) < 0.001


def test_parse_criteria_invalid_json():
    with pytest.raises(ValueError, match="criteria response"):
        parse_criteria_response("not json at all {bad")


# ── FORECASTER fixtures ───────────────────────────────────────────────────────

_FORECASTER_VALID = {
    "base_rate": 0.62,
    "base_rate_reference_class": "Fed meetings following prior hike with CPI >2.5%, 2000–2025",
    "base_rate_n": 14,
    "adjustments": [
        {
            "direction": "up",
            "magnitude_pp": 5,
            "source_url": "https://federalreserve.gov/newsevents",
            "rationale": "Recent FOMC minutes signal continued tightening bias.",
        }
    ],
    "p_yes": 0.67,
    "confidence": 0.72,
    "confidence_rationale": "Two independent primary sources with consistent signals.",
    "reasoning_trace": (
        "Reference class: Fed meetings with prior hike and CPI >2.5%. Historical YES "
        "rate 62% (n=14). One adjustment up 5pp for recent hawkish minutes. "
        "Final: 0.62 + 0.05 = 0.67."
    ),
    "unciteable": False,
}

_FORECASTER_NO_ADJUSTMENTS = {
    "base_rate": 0.50,
    "base_rate_reference_class": "Generic binary events",
    "base_rate_n": None,
    "adjustments": [],
    "p_yes": 0.50,
    "confidence": 0.30,
    "confidence_rationale": "No evidence fetched; pure base rate.",
    "reasoning_trace": "No sources available. Base rate only.",
    "unciteable": True,
}


def test_parse_forecaster_valid():
    data = parse_forecaster_response(json.dumps(_FORECASTER_VALID))
    assert abs(data["p_yes"] - 0.67) < 0.001
    assert abs(data["base_rate"] - 0.62) < 0.001
    assert data["base_rate_n"] == 14
    assert len(data["adjustments"]) == 1
    assert data["adjustments"][0]["direction"] == "up"
    assert abs(data["adjustments"][0]["magnitude_pp"] - 5) < 0.001
    assert data["unciteable"] is False


def test_parse_forecaster_no_adjustments():
    data = parse_forecaster_response(json.dumps(_FORECASTER_NO_ADJUSTMENTS))
    assert data["adjustments"] == []
    assert data["unciteable"] is True
    assert data["base_rate_n"] is None


def test_parse_forecaster_clamps_p_yes():
    clamped = dict(_FORECASTER_VALID, p_yes=1.5)
    data = parse_forecaster_response(json.dumps(clamped))
    assert data["p_yes"] == 0.99

    clamped_low = dict(_FORECASTER_VALID, p_yes=-0.1)
    data = parse_forecaster_response(json.dumps(clamped_low))
    assert data["p_yes"] == 0.01


def test_parse_forecaster_clamps_confidence():
    over = dict(_FORECASTER_VALID, confidence=2.0)
    data = parse_forecaster_response(json.dumps(over))
    assert data["confidence"] == 1.0


def test_parse_forecaster_sets_defaults():
    stripped = dict(_FORECASTER_VALID)
    del stripped["base_rate_n"]
    del stripped["confidence_rationale"]
    data = parse_forecaster_response(json.dumps(stripped))
    assert data["base_rate_n"] is None
    assert data["confidence_rationale"] == ""


def test_parse_forecaster_strips_json_fences():
    fenced = "```json\n" + json.dumps(_FORECASTER_VALID) + "\n```"
    data = parse_forecaster_response(fenced)
    assert abs(data["p_yes"] - 0.67) < 0.001


def test_parse_forecaster_invalid_json():
    with pytest.raises(ValueError, match="forecaster response"):
        parse_forecaster_response("{incomplete json")


def test_parse_forecaster_missing_required_fields():
    incomplete = {"base_rate": 0.5, "p_yes": 0.5}
    with pytest.raises(ValueError, match="missing fields"):
        parse_forecaster_response(json.dumps(incomplete))


# ── Format helpers ────────────────────────────────────────────────────────────

def test_format_criteria_prompt_substitutes_fields():
    system, user = format_criteria_prompt(
        question="Will rates rise?",
        criteria="Resolves YES if Fed raises rates.",
    )
    assert "Will rates rise?" in user
    assert "Resolves YES if Fed raises rates." in user
    assert len(system) > 100


def test_format_criteria_prompt_system_is_static():
    _, user1 = format_criteria_prompt("Question A?", "Criteria A.")
    _, user2 = format_criteria_prompt("Question B?", "Criteria B.")
    system1, _ = format_criteria_prompt("Question A?", "Criteria A.")
    system2, _ = format_criteria_prompt("Question B?", "Criteria B.")
    assert system1 == system2  # system block is identical across calls
    assert user1 != user2     # user block differs per market


def test_format_forecaster_prompt_substitutes_fields():
    doc = SourceDoc(
        url="https://federalreserve.gov/newsevents",
        fetched_at=datetime.utcnow(),
        content="The Fed held rates steady.",
        source_class="official_gov",
    )
    system, user = format_forecaster_prompt(
        question="Will rates rise?",
        criteria="Resolves YES if Fed raises rates.",
        category="Macro",
        resolution_clarity=0.85,
        edge_cases=["Meeting postponed"],
        source_docs=[doc],
        market_price=0.55,
    )
    assert "Will rates rise?" in user
    assert "0.85" in user
    assert "Meeting postponed" in user
    assert "federalreserve.gov" in user
    assert "0.550" in user
    assert len(system) > 100


def test_format_forecaster_prompt_system_is_static():
    system1, _ = format_forecaster_prompt(
        "Q1?", "C1.", "Politics", 0.8, [], [], 0.5
    )
    system2, _ = format_forecaster_prompt(
        "Q2?", "C2.", "Macro", 0.6, ["edge"], [], 0.7
    )
    assert system1 == system2  # system block is identical across calls


def test_format_forecaster_prompt_no_sources():
    system, user = format_forecaster_prompt(
        question="Will X happen?",
        criteria="Resolves YES if X.",
        category="Politics",
        resolution_clarity=0.6,
        edge_cases=[],
        source_docs=[],
        market_price=0.40,
    )
    assert "No primary sources fetched" in user
    assert "unciteable=true" in user
