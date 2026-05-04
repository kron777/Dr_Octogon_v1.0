"""
Whitelist-driven source fetcher.

fetch_for_market() → list[SourceDoc]

Flow:
  1. Google News RSS search — returns (google_redirect_url, title) pairs where
     the publisher domain (<source url> attribute) passes the whitelist check
  2. Fetch each Google redirect URL via httpx (follow_redirects=True lands on
     the real article); extract article text with BeautifulSoup
  3. If full article fetch fails (paywall, 403, JS-rendered), fall back to the
     title snippet — content is prefixed "[SNIPPET — full article unavailable]"
     so the forecaster knows the evidence quality
  4. SourceDoc.url is the final URL after redirects (real article URL)
  5. Cache content for CACHE_TTL_SECONDS keyed on input URL hash; rate-limit
     per domain

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

# Google GDPR consent bypass cookie — prevents landing on consent.google.com gate
_GOOGLE_CONSENT_COOKIES = {
    "SOCS": "CAISHAgBEhJnd3NfMjAyMzA4MDktMF9SQzEaAmVuIAEaBgiAnY2mBg=="
}

# Domains that indicate we hit a Google gate page rather than the real article
_GOOGLE_GATE_DOMAINS = {"consent.google.com", "news.google.com"}


async def fetch_for_market(market: MarketSnapshot) -> list[SourceDoc]:
    query = market.question

    # Google News RSS — pre-filtered by publisher domain (whitelist checked in search)
    # Returns (google_redirect_url, title) pairs; redirect resolves to real article
    candidates: list[tuple[str, str]] = []
    try:
        candidates = await _google_news_search(query)
    except Exception as exc:
        log.warning("sources.search_failed", market_id=market.market_id, error=str(exc))

    log.debug(
        "sources.candidates",
        market_id=market.market_id,
        whitelisted=len(candidates),
    )

    # Fetch concurrently with a semaphore to avoid hammering
    sem = asyncio.Semaphore(4)
    tasks = [
        _fetch_with_sem(sem, url, snippet)
        for url, snippet in candidates[:MAX_SOURCES_PER_MARKET]
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    docs: list[SourceDoc] = []
    for r in results:
        if isinstance(r, SourceDoc):
            docs.append(r)

    n_articles = sum(1 for d in docs if not d.content.startswith("[SNIPPET"))
    n_snippets = len(docs) - n_articles
    log.info(
        "sources.done",
        market_id=market.market_id,
        fetched=len(docs),
        articles=n_articles,
        snippets=n_snippets,
    )
    return docs


async def _google_news_search(query: str, n_results: int = 20) -> list[tuple[str, str]]:
    """
    Google News RSS search — no API key.
    Returns (google_redirect_url, title) pairs for whitelisted publishers only.

    RSS item structure:
      <link>   — Google redirect URL (news.google.com/rss/articles/...) → real article
      <source url="https://publisher.com"> — publisher domain for whitelist check
      <description> — HTML with <a href="same_google_redirect_url">

    We whitelist-check against <source url>, then return the Google redirect URL as the
    fetch target. httpx follow_redirects=True resolves it to the real article.
    """
    import xml.etree.ElementTree as ET

    site_filter = " OR ".join(
        f"site:{d}" for d in CONFIG.source_whitelist_domains[:8]
    )
    queries = [query, f"{query} {site_filter}"]
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=15, follow_redirects=True) as client:
        for q in queries:
            try:
                resp = await client.get(
                    "https://news.google.com/rss/search",
                    params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as exc:
                log.debug("sources.gnews_failed", query=q[:60], error=str(exc))
                continue

            for item in root.findall(".//item")[:n_results]:
                title_el = item.find("title")
                title = (title_el.text or "").strip() if title_el is not None else ""

                # Google redirect URL lives in <description> <a href> and also in <link>
                desc_el = item.find("description")
                google_url = None
                if desc_el is not None and desc_el.text:
                    desc_soup = BeautifulSoup(desc_el.text, "html.parser")
                    a = desc_soup.find("a", href=True)
                    if a:
                        google_url = a["href"]

                if not google_url or not google_url.startswith("http"):
                    continue

                # Whitelist check against publisher domain from <source url="...">, NOT
                # the Google redirect URL (which would never pass the whitelist check).
                source_el = item.find("source")
                publisher_url = (source_el.get("url", "") if source_el is not None else "")
                if not _is_whitelisted(publisher_url):
                    continue

                if google_url in seen:
                    continue
                seen.add(google_url)
                results.append((google_url, title))

    return results


async def _fetch_with_sem(
    sem: asyncio.Semaphore, url: str, snippet: str = ""
) -> SourceDoc | None:
    async with sem:
        return await _fetch_url(url, snippet)


async def _fetch_url(url: str, snippet: str = "") -> SourceDoc | None:
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

    content = ""
    final_url = url  # updated to real article URL after a clean redirect
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            cookies=_GOOGLE_CONSENT_COOKIES,
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            landing_domain = _get_domain(str(resp.url))
            if landing_domain not in _GOOGLE_GATE_DOMAINS:
                # Successfully reached the real article
                final_url = str(resp.url)
                content = _extract_text(resp.text)
            else:
                log.debug("sources.google_gate", url=url[:80], landing=landing_domain)
    except Exception as exc:
        log.debug("sources.fetch_failed", url=url[:80], error=str(exc))

    if not content.strip():
        if snippet.strip():
            content = f"[SNIPPET — full article unavailable] {snippet.strip()}"
            log.debug("sources.snippet_fallback", url=url[:80])
        else:
            return None

    final_domain = _get_domain(final_url)
    doc = SourceDoc(
        url=final_url,
        fetched_at=datetime.utcnow(),
        content=content[:MAX_CONTENT_CHARS],
        source_class=_classify_domain(final_domain),
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
                 "thehill.com", "rollcall.com", "npr.org",
                 "coindesk.com", "cointelegraph.com", "theblock.co"}


def _classify_domain(domain: str) -> SourceClass:
    if domain.endswith(".gov") or domain in _GOV_DOMAINS:
        return "official_gov"
    if domain in _NEWS_DOMAINS:
        return "primary_news"
    return "other"
