"""
Whitelist-driven source fetcher.

fetch_for_market() → list[SourceDoc]

Flow:
  1. DuckDuckGo news + text search scoped to whitelisted domains
  2. Filter results to whitelist; drop blacklisted domains silently
  3. Fetch each URL via httpx; extract article text with BeautifulSoup
  4. Cache content for CACHE_TTL_SECONDS keyed on URL hash; rate-limit per domain

.gov TLD is automatically whitelisted.
Anything in the blacklist is hard-dropped regardless of how the URL was found.
"""

import asyncio
import hashlib
import time
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from octagon.octagon_config import CONFIG
from octagon.octagon_models import MarketSnapshot, SourceDoc

log = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 3600
MAX_CONTENT_CHARS = 1200
FETCH_TIMEOUT_S = 10
MAX_SOURCES_PER_MARKET = 12

# module-level cache: {url_hash: (fetched_at_ts, SourceDoc)}
_cache: dict[str, tuple[float, SourceDoc]] = {}

# per-domain last-fetch timestamps for rate limiting
_domain_last_fetch: dict[str, float] = {}
DOMAIN_MIN_INTERVAL_S = 2.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def fetch_for_market(market: MarketSnapshot) -> list[SourceDoc]:
    query = market.question
    candidate_urls: list[str] = []

    # DuckDuckGo search — run in executor since the library is synchronous
    try:
        loop = asyncio.get_event_loop()
        urls = await loop.run_in_executor(None, _ddg_search, query)
        candidate_urls.extend(urls)
    except Exception as exc:
        log.warning("sources.ddg_failed", market_id=market.market_id, error=str(exc))

    # Filter to whitelist before fetching anything
    whitelisted = [u for u in candidate_urls if _is_whitelisted(u)]
    log.debug(
        "sources.candidates",
        market_id=market.market_id,
        total=len(candidate_urls),
        whitelisted=len(whitelisted),
    )

    # Fetch concurrently with a semaphore to avoid hammering
    sem = asyncio.Semaphore(4)
    tasks = [_fetch_with_sem(sem, url) for url in whitelisted[:MAX_SOURCES_PER_MARKET]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    docs: list[SourceDoc] = []
    for r in results:
        if isinstance(r, SourceDoc):
            docs.append(r)

    log.info(
        "sources.done",
        market_id=market.market_id,
        fetched=len(docs),
    )
    return docs


def _ddg_search(query: str) -> list[str]:
    from duckduckgo_search import DDGS
    urls: list[str] = []
    with DDGS() as ddgs:
        # Recent news
        try:
            for r in ddgs.news(query, max_results=15):
                if r.get("url"):
                    urls.append(r["url"])
        except Exception:
            pass
        # Broader text search scoped to known primary domains
        site_filter = " OR ".join(
            f"site:{d}" for d in CONFIG.source_whitelist_domains[:8]
        )
        try:
            for r in ddgs.text(f"{query} {site_filter}", max_results=10):
                if r.get("href"):
                    urls.append(r["href"])
        except Exception:
            pass
    return urls


async def _fetch_with_sem(sem: asyncio.Semaphore, url: str) -> SourceDoc | None:
    async with sem:
        return await _fetch_url(url)


async def _fetch_url(url: str) -> SourceDoc | None:
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

    # Cache hit
    if url_hash in _cache:
        ts, doc = _cache[url_hash]
        if time.time() - ts < CACHE_TTL_SECONDS:
            log.debug("sources.cache_hit", url=url[:80])
            return doc

    # Per-domain rate limit
    domain = _get_domain(url)
    last = _domain_last_fetch.get(domain, 0.0)
    wait = DOMAIN_MIN_INTERVAL_S - (time.time() - last)
    if wait > 0:
        await asyncio.sleep(wait)

    _domain_last_fetch[domain] = time.time()

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = _extract_text(resp.text)
    except Exception as exc:
        log.debug("sources.fetch_failed", url=url[:80], error=str(exc))
        return None

    if not content.strip():
        return None

    doc = SourceDoc(
        url=url,
        fetched_at=datetime.utcnow(),
        content=content[:MAX_CONTENT_CHARS],
        source_class=_classify_domain(domain),
    )
    _cache[url_hash] = (time.time(), doc)
    return doc


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # Prefer article/main containers; fall back to all paragraphs
    for tag in ("article", "main", '[role="main"]'):
        container = soup.find(tag)
        if container:
            paras = container.find_all("p")
            if paras:
                return " ".join(p.get_text(" ", strip=True) for p in paras)
    # Fallback: all <p> tags
    paras = soup.find_all("p")
    return " ".join(p.get_text(" ", strip=True) for p in paras[:40])


def _get_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _is_whitelisted(url: str) -> bool:
    domain = _get_domain(url)
    if not domain:
        return False

    # Blacklist takes priority
    for bl in CONFIG.source_blacklist_domains:
        if domain == bl or domain.endswith("." + bl):
            log.debug("sources.blacklisted", domain=domain)
            return False

    # .gov TLD auto-whitelisted
    if domain.endswith(".gov"):
        return True

    # Explicit whitelist
    for wl in CONFIG.source_whitelist_domains:
        if domain == wl or domain.endswith("." + wl):
            return True

    log.debug("sources.not_whitelisted", domain=domain)
    return False


SourceClass = Literal[
    "primary_news", "official_gov", "official_company", "primary_social", "other"
]

_GOV_DOMAINS = {"federalreserve.gov", "bls.gov", "census.gov", "sec.gov",
                "treasury.gov", "whitehouse.gov", "congress.gov",
                "ecb.europa.eu", "imf.org", "worldbank.org", "bis.org"}

_NEWS_DOMAINS = {"apnews.com", "reuters.com", "bloomberg.com", "ft.com",
                 "wsj.com", "nytimes.com", "washingtonpost.com",
                 "bbc.com", "bbc.co.uk", "theguardian.com",
                 "economist.com", "axios.com", "politico.com",
                 "thehill.com", "rollcall.com",
                 "coindesk.com", "cointelegraph.com", "theblock.co"}


def _classify_domain(domain: str) -> SourceClass:
    if domain.endswith(".gov") or domain in _GOV_DOMAINS:
        return "official_gov"
    if domain in _NEWS_DOMAINS:
        return "primary_news"
    return "other"
