"""
Cheap heuristic filter. No I/O. Must stay fast enough to handle 100+ markets per loop.
Every skip is DEBUG-logged with reason — that data is useful in Phase 1.
"""

from datetime import datetime
import structlog

from octagon.octagon_config import CONFIG
from octagon.octagon_models import MarketSnapshot, Prediction

log = structlog.get_logger(__name__)


def filter(
    markets: list[MarketSnapshot],
    recent: dict[str, Prediction],
) -> list[MarketSnapshot]:
    candidates = []
    for m in markets:
        reason = _should_skip(m, recent)
        if reason:
            log.debug("triage.skip", market_id=m.market_id, reason=reason, question=m.question[:60])
        else:
            candidates.append(m)
    log.info("triage.done", total=len(markets), candidates=len(candidates))
    return candidates


def _should_skip(m: MarketSnapshot, recent: dict[str, Prediction]) -> str | None:
    # Already evaluated within TTL and price hasn't moved enough to re-evaluate?
    if m.market_id in recent:
        pred = recent[m.market_id]
        age = (datetime.utcnow() - pred.predicted_at).total_seconds()
        price_delta = abs(m.yes_price - pred.market_price_at_prediction)
        if age < pred.ttl_seconds and price_delta < CONFIG.repredict_threshold:
            return f"within_ttl age={age:.0f}s price_delta={price_delta:.3f}"

    # Not enough liquidity to trade meaningfully
    if m.depth_yes_usd < CONFIG.min_depth_usd:
        return f"depth_yes_too_low={m.depth_yes_usd:.0f}"
    if m.depth_no_usd < CONFIG.min_depth_usd:
        return f"depth_no_too_low={m.depth_no_usd:.0f}"

    # Wide spread signals adverse-selection risk
    spread = m.ask_yes - m.bid_yes
    if spread > CONFIG.max_spread:
        return f"spread_too_wide={spread:.3f}"

    # Resolution horizon filter
    now = datetime.utcnow()
    hours_to_resolution = (m.resolves_at - now).total_seconds() / 3600
    if hours_to_resolution < CONFIG.min_hours_to_resolution:
        return f"resolves_too_soon={hours_to_resolution:.1f}h"
    if hours_to_resolution > CONFIG.max_hours_to_resolution:
        return f"resolves_too_far={hours_to_resolution:.1f}h"

    # Category whitelist — case-insensitive
    category_lower = m.category.lower()
    allowed_lower = [c.lower() for c in CONFIG.allowed_categories]
    if category_lower not in allowed_lower:
        return f"category_not_whitelisted={m.category!r}"

    return None
