"""
octagon_research.py — one public method: evaluate().

Pipeline per market:
  1. Criteria parse (CRITERIA_PARSER, haiku via Anthropic) — operational risk assessment
  2. Source fetch (octagon_sources) — gather whitelisted evidence
  3. Forecaster call (Qwen-3-235B via Cerebras, OpenAI-compatible endpoint)
  4. Validation — arithmetic check + URL citation check → flag unciteable
  5. Calibration adjust (Phase 0 stub)
  6. Assemble Prediction; write trace file to disk

Criteria call uses Anthropic prompt caching (cache_control=ephemeral) on the
static system block. Forecaster call uses OpenAI-compatible client pointed at
https://api.cerebras.ai/v1 — no Anthropic cache headers on this path.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import structlog
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

import octagon.octagon_calibration as calibration
import octagon.octagon_sources as sources
from octagon.octagon_config import CONFIG
from octagon.octagon_models import Adjustment, MarketSnapshot, Prediction, SourceDoc
from octagon.octagon_prompts import (
    format_criteria_prompt,
    format_forecaster_prompt,
    parse_criteria_response,
    parse_forecaster_response,
)

log = structlog.get_logger(__name__)

_ARITHMETIC_TOLERANCE = 0.03


async def evaluate(market: MarketSnapshot) -> Prediction:
    """Full research pipeline for one market. Returns a Prediction."""
    anthropic_client = AsyncAnthropic(api_key=CONFIG.anthropic_api_key)
    cerebras_client = AsyncOpenAI(
        api_key=CONFIG.cerebras_api_key,
        base_url=CONFIG.cerebras_base_url,
    )

    # Step 1: Resolution-criteria parse
    criteria_data = await _parse_criteria(anthropic_client, cerebras_client, market)
    resolution_clarity: float = criteria_data["resolution_clarity"]
    edge_cases: list[str] = criteria_data.get("edge_cases", [])

    # Step 2: Source fetch
    source_docs: list[SourceDoc] = await sources.fetch_for_market(market)

    # Step 3: Forecaster (Cerebras / Qwen-3-235B)
    forecast = await _run_forecaster(
        cerebras_client, market, resolution_clarity, edge_cases, source_docs
    )

    # Step 4: Validation
    fetched_urls = {doc.url for doc in source_docs}
    unciteable = _validate(forecast, fetched_urls)

    # Step 5: Calibration adjust
    p_raw: float = forecast["p_yes"]
    p_yes = calibration.adjust(p_raw, market.category)

    # Step 6: Assemble
    adjustments = [
        Adjustment(
            direction=a["direction"],
            magnitude_pp=float(a["magnitude_pp"]),
            source_url=a.get("source_url", ""),
            rationale=a.get("rationale", ""),
        )
        for a in forecast.get("adjustments", [])
    ]

    confidence = _assemble_confidence(
        raw_confidence=forecast["confidence"],
        n_sources=len(source_docs),
        resolution_clarity=resolution_clarity,
    )

    prediction_id = Prediction.new_id()
    trace_path = _write_trace(prediction_id, market, forecast, criteria_data, source_docs)

    prediction = Prediction(
        prediction_id=prediction_id,
        market_id=market.market_id,
        p_yes=p_yes,
        p_yes_raw=p_raw,
        base_rate=forecast["base_rate"],
        base_rate_reference_class=forecast["base_rate_reference_class"],
        adjustments=adjustments,
        confidence=confidence,
        edge=p_yes - market.yes_price,
        reasoning_trace_path=str(trace_path),
        evidence_refs=[doc.url for doc in source_docs],
        market_price_at_prediction=market.yes_price,
        resolution_clarity=resolution_clarity,
        edge_cases=edge_cases,
        unciteable=unciteable,
        predicted_at=datetime.utcnow(),
        ttl_seconds=CONFIG.ttl_seconds,
        model_used=f"{CONFIG.forecaster_model}-cerebras",
    )

    log.info(
        "research.done",
        market_id=market.market_id,
        p_yes=round(p_yes, 3),
        edge=round(prediction.edge, 3),
        confidence=round(confidence, 3),
        unciteable=unciteable,
        sources=len(source_docs),
    )
    return prediction


async def _parse_criteria(
    anthropic_client: AsyncAnthropic,
    cerebras_client: AsyncOpenAI,
    market: MarketSnapshot,
) -> dict:
    system, user = format_criteria_prompt(market.question, market.resolution_criteria)
    try:
        if CONFIG.criteria_provider == "cerebras":
            response = await cerebras_client.chat.completions.create(
                model=CONFIG.criteria_model,
                max_tokens=512,
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = response.choices[0].message.content
        else:
            msg = await anthropic_client.messages.create(
                model=CONFIG.criteria_model,
                max_tokens=512,
                temperature=0,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
            text = msg.content[0].text
        return parse_criteria_response(text)
    except Exception as exc:
        log.warning("research.criteria_failed", market_id=market.market_id, error=str(exc))
        return {
            "resolution_clarity": 0.5,
            "edge_cases": [],
            "external_dependencies": [],
            "clarity_reasoning": "",
        }


async def _run_forecaster(
    cerebras_client: AsyncOpenAI,
    market: MarketSnapshot,
    resolution_clarity: float,
    edge_cases: list[str],
    source_docs: list[SourceDoc],
) -> dict:
    from openai import RateLimitError

    system, user = format_forecaster_prompt(
        question=market.question,
        criteria=market.resolution_criteria,
        category=market.category,
        resolution_clarity=resolution_clarity,
        edge_cases=edge_cases,
        source_docs=source_docs,
        market_price=market.yes_price,
    )
    try:
        response = await cerebras_client.chat.completions.create(
            model=CONFIG.forecaster_model,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except RateLimitError as exc:
        log.warning(
            "research.forecaster_rate_limited",
            market_id=market.market_id,
            model=CONFIG.forecaster_model,
            error=str(exc),
        )
        raise

    usage = response.usage
    log.debug(
        "forecaster_usage",
        market_id=market.market_id,
        model=CONFIG.forecaster_model,
        input=getattr(usage, "prompt_tokens", 0),
        output=getattr(usage, "completion_tokens", 0),
    )
    return parse_forecaster_response(response.choices[0].message.content)


def _validate(forecast: dict, fetched_urls: set[str]) -> bool:
    """
    Return True (unciteable) iff any code-level check fails.

    Checks (in order):
      1. base_rate was null  — model had no reference class at all
      2. Arithmetic          — p_yes must equal base_rate + Σ signed adjustments / 100
      3. Citation            — every adjustment must cite a URL we actually fetched

    We do NOT blindly trust the model's own unciteable flag.  When the model sets
    unciteable=true despite having a valid base_rate and passing arithmetic, it is being
    over-cautious; our code-level checks are the authoritative gate.
    """
    # 1. No reference class at all (base_rate returned as null by model)
    if forecast.get("base_rate_was_null", False):
        log.debug("research.no_reference_class")
        return True

    # 2. Arithmetic
    base = forecast["base_rate"]
    signed_pp = sum(
        (a["magnitude_pp"] if a["direction"] == "up" else -a["magnitude_pp"])
        for a in forecast.get("adjustments", [])
    )
    expected = max(0.01, min(0.99, base + signed_pp / 100))
    p_yes = forecast["p_yes"]

    if abs(p_yes - expected) > _ARITHMETIC_TOLERANCE:
        log.warning(
            "research.arithmetic_mismatch",
            p_yes=p_yes,
            expected=round(expected, 4),
            delta=round(abs(p_yes - expected), 4),
        )
        return True

    # 3. Citation
    for adj in forecast.get("adjustments", []):
        url = adj.get("source_url", "")
        if not url or url not in fetched_urls:
            log.warning("research.uncited_adjustment", source_url=url[:80])
            return True

    return False


def _assemble_confidence(
    raw_confidence: float,
    n_sources: int,
    resolution_clarity: float,
) -> float:
    source_factor = min(1.0, n_sources / 5.0)
    assembled = raw_confidence * 0.6 + source_factor * 0.2 + resolution_clarity * 0.2
    return round(max(0.0, min(1.0, assembled)), 4)


def _write_trace(
    prediction_id: str,
    market: MarketSnapshot,
    forecast: dict,
    criteria_data: dict,
    source_docs: list[SourceDoc],
) -> Path:
    trace_dir = Path(os.path.expanduser(CONFIG.trace_dir))
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{prediction_id}.json"
    trace_path.write_text(
        json.dumps(
            {
                "prediction_id": prediction_id,
                "market_id": market.market_id,
                "question": market.question,
                "criteria_parse": criteria_data,
                "forecast": forecast,
                "source_urls": [doc.url for doc in source_docs],
                "traced_at": datetime.utcnow().isoformat(),
            },
            indent=2,
        )
    )
    return trace_path
