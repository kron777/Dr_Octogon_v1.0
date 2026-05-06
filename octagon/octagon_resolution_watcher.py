"""
Resolution watcher — runs hourly, writes outcomes for expired markets.

Fetches each expired market from the Gamma API. A market is considered resolved
when closed=true and outcomePrices shows [1, 0] or [0, 1]. INVALID is written
for cancelled, disputed, or ambiguous payouts.

Phase 0: pure data collection. Triggers nothing downstream.
"""

import asyncio
import json as _json
from datetime import datetime

import httpx
import structlog

from octagon.octagon_ledger import OctagonLedger
from octagon.octagon_models import Resolution
from octagon.octagon_pnl import compute_paper_pnl
from octagon.octagon_scanner import GAMMA_BASE, _get_with_retry

log = structlog.get_logger(__name__)

RESOLUTION_CONCURRENCY = 5


async def run(ledger: OctagonLedger) -> None:
    pending = ledger.unresolved_markets_past_resolution_time()
    invalid_pending = ledger.invalid_resolution_market_ids()

    # De-duplicate: invalid_pending might overlap with pending if ledger state is inconsistent
    recheck = [mid for mid in invalid_pending if mid not in set(pending)]

    total = len(pending) + len(recheck)
    if not total:
        log.info("watcher.no_pending")
        return

    log.info("watcher.start", pending=len(pending), recheck_invalid=len(recheck))
    sem = asyncio.Semaphore(RESOLUTION_CONCURRENCY)
    tasks = [_check_market(sem, ledger, mid) for mid in pending + recheck]
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("watcher.done", checked=total)


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
            _close_open_trades(ledger, market_id, resolution.outcome, resolution.resolved_at)
        else:
            log.debug("watcher.not_resolved_yet", market_id=market_id)


def _close_open_trades(
    ledger: OctagonLedger, market_id: str, outcome: str, resolved_at: datetime
) -> None:
    trades = ledger.get_open_trades_for_market(market_id)
    for trade in trades:
        pnl = compute_paper_pnl(trade, outcome)
        ledger.close_trade(trade.trade_id, pnl=pnl, closed_at=resolved_at)
        log.info(
            "watcher.trade_closed",
            market_id=market_id,
            trade_id=trade.trade_id,
            side=trade.side,
            outcome=outcome,
            pnl_usd=round(pnl, 4),
        )


def _extract_resolution(market_id: str, data: dict) -> Resolution | None:
    closed = data.get("closed") or data.get("resolved")
    if not closed:
        return None

    # Gamma API returns outcomePrices and payout as JSON-encoded strings, not lists.
    # Mirror the scanner's parsing (octagon_scanner.py) to avoid iterating characters.
    outcome_prices = data.get("outcomePrices") or []
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = _json.loads(outcome_prices)
        except (_json.JSONDecodeError, ValueError):
            outcome_prices = []

    payout = data.get("payout") or []
    if isinstance(payout, str):
        try:
            payout = _json.loads(payout)
        except (_json.JSONDecodeError, ValueError):
            payout = []

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
