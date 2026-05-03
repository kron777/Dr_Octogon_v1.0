"""
evaluate() — the research pipeline for a single market.

Five steps:
  1. Resolution-criteria parse  → resolution_clarity, edge_cases   (cheap model)
  2. Source fetch               → list[SourceDoc]
  3. Probability estimation     → Claude structured JSON             (main model)
  4. Calibration adjustment     → stub in Phase 0
  5. Confidence assembly        → penalise for resolution ambiguity

Never crashes the main loop. On any Claude failure, returns a Prediction
with unciteable=True and confidence=0.0 so the calibration log can track failures.
"""

import json
from datetime import datetime
import structlog
import anthropic

from octagon.octagon_config import CONFIG
from octagon.octagon_models import MarketSnapshot, Prediction, SourceDoc
from octagon.octagon_research.prompts import (
    format_criteria_prompt,
    format_research_prompt,
    parse_criteria_response,
    parse_research_response,
    category_base_rate,
)
from octagon import octagon_calibration
from octagon import octagon_sources

log = structlog.get_logger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=CONFIG.anthropic_api_key)
    return _client


async def _call_claude(prompt: str, model: str, max_tokens: int = 1024) -> str:
    client = _get_client()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def _parse_criteria(market: MarketSnapshot) -> tuple[float, list[dict]]:
    prompt = format_criteria_prompt(market.question, market.resolution_criteria)
    try:
        raw = await _call_claude(prompt, CONFIG.criteria_model, max_tokens=512)
        data = parse_criteria_response(raw)
        return data["resolution_clarity"], data["edge_cases"]
    except Exception as exc:
        log.warning(
            "research.criteria_parse_failed",
            market_id=market.market_id,
            error=str(exc),
        )
        return 0.5, []


async def evaluate(market: MarketSnapshot) -> Prediction:
    pid = Prediction.new_id()
    predicted_at = datetime.utcnow()

    log.info(
        "research.start",
        market_id=market.market_id,
        question=market.question[:80],
        yes_price=market.yes_price,
    )

    # Step 1: resolution-criteria assessment
    resolution_clarity, criteria_edge_cases = await _parse_criteria(market)
    log.info(
        "research.criteria_done",
        market_id=market.market_id,
        clarity=resolution_clarity,
        edge_cases=len(criteria_edge_cases),
    )

    # Step 2: source fetch
    source_docs: list[SourceDoc] = []
    try:
        source_docs = await octagon_sources.fetch_for_market(market)
    except Exception as exc:
        log.warning("research.sources_failed", market_id=market.market_id, error=str(exc))

    log.info(
        "research.sources_done",
        market_id=market.market_id,
        n_sources=len(source_docs),
    )

    # Step 3: probability estimation
    prompt = format_research_prompt(
        question=market.question,
        criteria=market.resolution_criteria,
        category=market.category,
        resolution_clarity=resolution_clarity,
        edge_cases=criteria_edge_cases,
        source_docs=source_docs,
        market_price=market.yes_price,
    )

    try:
        raw = await _call_claude(prompt, CONFIG.research_model, max_tokens=2048)
        result = parse_research_response(raw)
    except Exception as exc:
        log.error(
            "research.claude_failed",
            market_id=market.market_id,
            error=str(exc),
        )
        return _failed_prediction(pid, market, predicted_at, resolution_clarity)

    p_yes_raw = result["p_yes"]
    unciteable = bool(result.get("unciteable", False))

    if unciteable:
        log.warning(
            "research.unciteable",
            market_id=market.market_id,
            p_yes_raw=p_yes_raw,
            base_rate_source=result.get("base_rate", {}).get("source"),
        )

    # Step 4: calibration adjustment (Phase 0 stub — returns p unchanged)
    p_yes = octagon_calibration.adjust(p_yes_raw, market.category)

    # Step 5: confidence assembly
    # Resolution ambiguity penalises confidence regardless of Claude's stated confidence.
    # At clarity=0 confidence is at most 30% of Claude's estimate; at clarity=1 it's unchanged.
    raw_confidence = result["confidence"]
    confidence = raw_confidence * (0.3 + 0.7 * resolution_clarity)
    # If unciteable, cap confidence further — the estimate has no grounded base rate.
    if unciteable:
        confidence = min(confidence, 0.3)

    edge = p_yes - market.yes_price
    base = result.get("base_rate", {})

    log.info(
        "research.done",
        market_id=market.market_id,
        p_yes=round(p_yes, 3),
        confidence=round(confidence, 3),
        edge=round(edge, 3),
        unciteable=unciteable,
    )

    return Prediction(
        prediction_id=pid,
        market_id=market.market_id,
        p_yes=p_yes,
        p_yes_raw=p_yes_raw,
        confidence=confidence,
        edge=edge,
        reasoning=result.get("reasoning_trace", ""),
        evidence_refs=[doc.url for doc in source_docs],
        market_price_at_prediction=market.yes_price,
        resolution_clarity=resolution_clarity,
        unciteable=unciteable,
        predicted_at=predicted_at,
        ttl_seconds=CONFIG.ttl_seconds,
        base_rate=float(base.get("value", category_base_rate(market.category))),
        base_rate_ref_class=base.get("reference_class", ""),
        base_rate_source=base.get("source", ""),
        adjustments=result.get("adjustments", []),
        edge_cases_considered=result.get("edge_cases_considered", []) + criteria_edge_cases,
    )


def _failed_prediction(
    pid: str,
    market: MarketSnapshot,
    predicted_at: datetime,
    resolution_clarity: float,
) -> Prediction:
    """Returned when Claude fails. Logged so failures appear in calibration stats."""
    return Prediction(
        prediction_id=pid,
        market_id=market.market_id,
        p_yes=0.5,
        p_yes_raw=0.5,
        confidence=0.0,
        edge=0.5 - market.yes_price,
        reasoning="Claude call failed — see logs.",
        evidence_refs=[],
        market_price_at_prediction=market.yes_price,
        resolution_clarity=resolution_clarity,
        unciteable=True,
        predicted_at=predicted_at,
        ttl_seconds=CONFIG.ttl_seconds,
        base_rate=category_base_rate(market.category),
        base_rate_ref_class="",
        base_rate_source="none",
        adjustments=[],
        edge_cases_considered=[],
    )
