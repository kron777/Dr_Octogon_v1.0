"""
Resolution watcher — runs hourly, writes outcomes for expired markets.

Fetches each expired market from the Gamma API. A market is considered resolved
when closed=true and outcomePrices shows [1, 0] or [0, 1]. INVALID is written
for cancelled, disputed, or ambiguous payouts.

Phase 0: pure data collection. Triggers nothing downstream.
"""

import asyncio
from datetime import datetime

import httpx
import structlog

from octagon.octagon_ledger import OctagonLedger
from octagon.octagon_models import Resolution
from octagon.octagon_scanner import GAMMA_BASE, _get_with_retry

log = structlog.get_logger(__name__)

RESOLUTION_CONCURRENCY = 5


async def run(ledger: OctagonLedger) -> None:
    pending = ledger.unresolved_markets_past_resolution_time()
    if not pending:
        log.info("watcher.no_pending")
        return

    log.info("watcher.start", pending=len(pending))
    sem = asyncio.Semaphore(RESOLUTION_CONCURRENCY)
    tasks = [_check_market(sem, ledger, mid) for mid in pending]
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("watcher.done", checked=len(pending))


async def _check_market(
    sem: asyncio.Semaphore, ledger: OctagonLedger, market_id: str
) -> None:
    async with sem:
        async with httpx.AsyncClient(timeout=15) as client:
            data = await _get_with_retry(
                client, f"{GAMMA_BASE}/markets/{market_id}"
            )
        if not data:
            log.warning("watcher.fetch_failed", market_id=market_id)
            return

        resolution = _extract_resolution(market_id, data)
        if resolution:
            ledger.log_resolution(resolution)
        else:
            log.debug("watcher.not_resolved_yet", market_id=market_id)


def _extract_resolution(market_id: str, data: dict) -> Resolution | None:
    closed = data.get("closed") or data.get("resolved")
    if not closed:
        return None

    outcome_prices = data.get("outcomePrices") or []
    payout = data.get("payout") or []

    # Prefer explicit payout array; fall back to outcomePrices
    vals = payout if payout else outcome_prices
    try:
        floats = [float(v) for v in vals]
    except (ValueError, TypeError):
        floats = []

    if floats and abs(floats[0] - 1.0) < 0.01:
        outcome = "YES"
    elif len(floats) > 1 and abs(floats[1] - 1.0) < 0.01:
        outcome = "NO"
    else:
        outcome = "INVALID"

    resolved_at_raw = (
        data.get("resolutionTime")
        or data.get("updatedAt")
        or data.get("endDate")
    )
    try:
        resolved_at = datetime.fromisoformat(
            str(resolved_at_raw).replace("Z", "")
        )
    except Exception:
        resolved_at = datetime.utcnow()

    return Resolution(
        market_id=market_id,
        outcome=outcome,  # type: ignore[arg-type]
        resolved_at=resolved_at,
        source="polymarket_gamma_api",
    )
