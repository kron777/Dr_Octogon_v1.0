"""
Polymarket market discovery.

Two APIs:
  Gamma API   — market metadata, prices, categories (public, no auth)
  CLOB API    — order book bid/ask/depth (public, no auth, no signing)

scan() returns all active markets that survive basic sanity checks.
Book data is fetched per-market; failures fall back to Gamma's mid price
with estimated spread rather than dropping the market.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from octagon.octagon_config import CONFIG
from octagon.octagon_models import MarketSnapshot

log = structlog.get_logger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
PAGE_SIZE = 100
MAX_BOOK_CONCURRENCY = 10
REQUEST_TIMEOUT = 15

# Logged once per scan so Jon can tune the category whitelist in Phase 1
_seen_categories: set[str] = set()


async def scan() -> list[MarketSnapshot]:
    markets_raw = await _fetch_all_markets()
    log.info("scanner.gamma_fetched", count=len(markets_raw))

    # Fetch order books concurrently
    sem = asyncio.Semaphore(MAX_BOOK_CONCURRENCY)
    tasks = [_enrich_with_book(sem, m) for m in markets_raw]
    snapshots_or_none = await asyncio.gather(*tasks, return_exceptions=True)

    snapshots = [s for s in snapshots_or_none if isinstance(s, MarketSnapshot)]
    log.info(
        "scanner.done",
        enriched=len(snapshots),
        skipped=len(markets_raw) - len(snapshots),
        categories_seen=sorted(_seen_categories),
    )
    return snapshots


async def _fetch_all_markets() -> list[dict]:
    results: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        while len(results) < CONFIG.max_markets_per_scan:
            page = await _get_with_retry(
                client,
                f"{GAMMA_BASE}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            if not page:
                break
            results.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    return results[: CONFIG.max_markets_per_scan]


async def _enrich_with_book(sem: asyncio.Semaphore, raw: dict) -> MarketSnapshot | None:
    async with sem:
        return await _build_snapshot(raw)


async def _build_snapshot(raw: dict) -> MarketSnapshot | None:
    market_id = raw.get("id") or raw.get("conditionId") or raw.get("marketMakerAddress")
    if not market_id:
        return None

    question = (raw.get("question") or "").strip()
    if not question:
        return None

    category = (
        raw.get("category")
        or raw.get("groupItemTitle")
        or _infer_category(raw)
        or "Other"
    )
    _seen_categories.add(category)

    resolution_criteria = (
        raw.get("description")
        or raw.get("rules")
        or raw.get("resolutionSource")
        or ""
    ).strip()

    # Parse resolution time — Gamma uses ISO strings or Unix timestamps
    resolves_at = _parse_date(raw.get("endDate") or raw.get("gameStartTime"))
    if resolves_at is None:
        return None

    # Base price from Gamma outcome prices
    outcome_prices = raw.get("outcomePrices") or []
    try:
        yes_price_mid = float(outcome_prices[0]) if outcome_prices else 0.5
    except (IndexError, ValueError, TypeError):
        yes_price_mid = 0.5

    # Order book
    clob_token_ids = raw.get("clobTokenIds") or []
    yes_token_id = clob_token_ids[0] if clob_token_ids else None

    bid_yes = yes_price_mid - 0.01
    ask_yes = yes_price_mid + 0.01
    depth_yes_usd = 0.0
    depth_no_usd = 0.0

    if yes_token_id:
        book = await _fetch_book(yes_token_id)
        if book:
            bid_yes, ask_yes, depth_yes_usd, depth_no_usd = _parse_book(book, yes_price_mid)

    volume_24h = float(raw.get("volume24hr") or raw.get("volume24h") or 0)

    return MarketSnapshot(
        market_id=str(market_id),
        question=question,
        yes_price=yes_price_mid,
        bid_yes=max(0.01, bid_yes),
        ask_yes=min(0.99, ask_yes),
        depth_yes_usd=depth_yes_usd,
        depth_no_usd=depth_no_usd,
        volume_24h=volume_24h,
        resolves_at=resolves_at,
        resolution_criteria=resolution_criteria,
        category=category,
        snapshot_at=datetime.utcnow(),
    )


async def _fetch_book(token_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        data = await _get_with_retry(
            client, f"{CLOB_BASE}/book", params={"token_id": token_id}
        )
        return data if isinstance(data, dict) else None


def _parse_book(
    book: dict, mid: float
) -> tuple[float, float, float, float]:
    """Return (bid_yes, ask_yes, depth_yes_usd, depth_no_usd)."""
    depth_window = CONFIG.depth_cents / 100.0

    bids: list[dict] = book.get("bids") or []
    asks: list[dict] = book.get("asks") or []

    best_bid = max((float(b["price"]) for b in bids if b.get("price")), default=mid - 0.01)
    best_ask = min((float(a["price"]) for a in asks if a.get("price")), default=mid + 0.01)

    depth_yes = sum(
        float(b["price"]) * float(b["size"])
        for b in bids
        if b.get("price") and b.get("size") and (mid - float(b["price"])) <= depth_window
    )
    # YES asks = NO bids; convert to NO-side USD
    depth_no = sum(
        (1.0 - float(a["price"])) * float(a["size"])
        for a in asks
        if a.get("price") and a.get("size") and (float(a["price"]) - mid) <= depth_window
    )

    return best_bid, best_ask, depth_yes, depth_no


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    attempts: int = 3,
) -> Any:
    delay = 1.0
    for attempt in range(attempts):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                log.warning("scanner.rate_limited", url=url, attempt=attempt + 1)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning("scanner.http_error", url=url, status=exc.response.status_code)
            return None
        except Exception as exc:
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
            else:
                log.warning("scanner.request_failed", url=url, error=str(exc))
    return None


def _parse_date(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    if isinstance(val, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return None


def _infer_category(raw: dict) -> str:
    """Heuristic category from tags if the top-level field is missing."""
    tags = raw.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            name = (tag.get("label") or tag.get("name") or "").strip()
            if name:
                return name
    return ""
