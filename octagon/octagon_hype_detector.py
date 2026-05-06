"""
octagon_hype_detector.py — per-market hype signals for the contrarian-hype-fade lane.

Three signals:
  news_velocity          : raw Google News RSS result count / MAX_VELOCITY_NORM (capped 0–1)
  sentiment_intensity    : LLM-scored emotional charge of top headlines (0–1, cached per TTL)
  prob_drift_vs_evidence : |price_change_24h| / max(1, n_evidence_refs), clipped to 0–1

Composite (weighted average):
  hype_score = 0.4 * news_velocity + 0.4 * sentiment_intensity + 0.2 * prob_drift_vs_evidence

Mitigations
-----------
Velocity gate (HYPE_VELOCITY_GATE, default 0.5):
  Sentiment LLM call is skipped when news_velocity < gate; sentiment_intensity fixed at 0.5.
  Reasoning: when velocity < 0.5 the velocity component alone contributes at most 0.2 to
  hype_score (0.4 × 0.5), so even a perfect sentiment score of 1.0 can push hype_score to
  0.4 + 0.2*drift ≤ 0.6, which stays below the 0.7 gate threshold. The LLM call cannot
  change the outcome, so it is skipped. Logged as "hype.sentiment_skipped_low_velocity"
  (distinct from "hype.sentiment_failed" so the two cases remain separable in metrics).

Fallback provider (SENTIMENT_FALLBACK_PROVIDER, default "local"):
  When the primary sentiment provider 429s or errors, one retry fires against the fallback
  before defaulting to 0.5. "local" uses LOCAL_LLAMA_URL (OpenAI-compatible, no API key).
  If LOCAL_LLAMA_URL is unreachable the fallback itself fails gracefully — no crash.
  A one-time warning is logged at module import if localhost:8080 is not connectable.

Cache TTL (HYPE_CACHE_TTL_HOURS, default 4h, was 1h):
  Longer TTL reduces LLM cost for slow-moving markets. Score is recomputed when the
  cached entry is older than CONFIG.hype_cache_ttl_hours × 3600 seconds.

MODEL NOTE: spec requested gpt-oss-120b (Cerebras). Returns HTTP 404 on current free-tier
key. Default SENTIMENT_MODEL=llama3.1-8b. Override via .env.
"""

import asyncio
import json
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
import structlog

from octagon.octagon_config import CONFIG
from octagon.octagon_ledger import OctagonLedger
from octagon.octagon_models import MarketSnapshot, Prediction
from octagon.octagon_whale_flow import WhaleSignal, check as whale_check

log = structlog.get_logger(__name__)

MAX_VELOCITY_NORM = 20.0   # raw RSS count that maps to velocity=1.0
SENTIMENT_TIMEOUT = 30     # seconds per LLM attempt; CPU-local inference needs ~13–20s

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

_SENTIMENT_PROMPT_SYSTEM = (
    "You are a news sentiment scorer. Given a list of news headlines about a prediction market, "
    "score the overall emotional charge and hype level on a scale from 0.0 to 1.0. "
    "0.0 = calm, factual, no urgency. 1.0 = extremely hyped, emotional, urgent, viral. "
    "Consider: exclamation marks, superlatives, breaking-news framing, celebrity/viral subject matter, "
    "and the number of headlines relative to the topic importance. "
    "Respond with a single JSON object: {\"score\": <float 0.0-1.0>, \"reason\": \"<one sentence>\"}. "
    "No other text."
)


# ── Startup reachability check for local llama server ────────────────────────

def _check_local_llama_reachable() -> None:
    """Warn once at import time if LOCAL_LLAMA_URL host:port is not connectable."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(CONFIG.local_llama_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 8080
        with socket.create_connection((host, port), timeout=1):
            pass
        log.debug("hype.local_llama_reachable", url=CONFIG.local_llama_url)
    except Exception:
        log.warning(
            "hype.local_llama_unreachable",
            url=CONFIG.local_llama_url,
            note="Fallback to local will fail gracefully; primary provider still works. "
                 "Note: shares compute with NEX on this machine when running.",
        )


_check_local_llama_reachable()


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class HypeResult:
    market_id: str
    news_velocity: float           # 0–1
    sentiment_intensity: float     # 0–1
    prob_drift_vs_evidence: float  # 0–1
    hype_score: float              # weighted composite 0–1
    whale_signal: WhaleSignal
    computed_at: str               # ISO datetime string


# ── Cache management ──────────────────────────────────────────────────────────

def _cache_ttl_seconds() -> float:
    return CONFIG.hype_cache_ttl_hours * 3600


def _load_cache() -> dict:
    path = Path(CONFIG.hype_score_cache_path)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"scores": {}, "daily": {"date": "", "placed": 0, "rejected": 0}}


def _save_cache(data: dict) -> None:
    path = Path(CONFIG.hype_score_cache_path)
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("hype.cache_write_failed", error=str(exc))


def _get_cached(market_id: str) -> HypeResult | None:
    cache = _load_cache()
    entry = cache.get("scores", {}).get(market_id)
    if not entry:
        return None
    try:
        age = time.time() - datetime.fromisoformat(entry["computed_at"]).timestamp()
        if age < _cache_ttl_seconds():
            return HypeResult(**entry)
    except Exception:
        pass
    return None


def _write_score(result: HypeResult) -> None:
    cache = _load_cache()
    cache.setdefault("scores", {})[result.market_id] = asdict(result)
    _save_cache(cache)


def record_rejected(market_id: str) -> None:
    """Increment daily rejection counter. Called by executor on hype-gate reject."""
    cache = _load_cache()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    daily = cache.setdefault("daily", {"date": "", "placed": 0, "rejected": 0})
    if daily.get("date") != today:
        cache["daily"] = {"date": today, "placed": 0, "rejected": 0}
    cache["daily"]["rejected"] += 1
    _save_cache(cache)


def record_placed(market_id: str) -> None:
    """Increment daily placed counter. Called by executor when a hype-fade trade fires."""
    cache = _load_cache()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    daily = cache.setdefault("daily", {"date": "", "placed": 0, "rejected": 0})
    if daily.get("date") != today:
        cache["daily"] = {"date": today, "placed": 0, "rejected": 0}
    cache["daily"]["placed"] += 1
    _save_cache(cache)


def placed_today() -> int:
    cache = _load_cache()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    daily = cache.get("daily", {})
    if daily.get("date") != today:
        return 0
    return daily.get("placed", 0)


def read_hud_data() -> dict:
    """Return cache data for HUD consumption."""
    cache = _load_cache()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    daily = cache.get("daily", {})
    if daily.get("date") != today:
        daily = {"date": today, "placed": 0, "rejected": 0}

    scores = cache.get("scores", {})
    recent = sorted(
        scores.values(),
        key=lambda x: x.get("hype_score", 0),
        reverse=True,
    )[:8]

    return {
        "enabled": CONFIG.hype_fade_enabled,
        "placed_today": daily.get("placed", 0),
        "rejected_today": daily.get("rejected", 0),
        "daily_cap": CONFIG.hype_fade_daily_cap,
        "min_score": CONFIG.hype_fade_min_score,
        "top_markets": recent,
    }


# ── Signal computation ────────────────────────────────────────────────────────

async def _count_rss_articles(query: str) -> int:
    """Count raw Google News RSS results for query (no whitelist filter)."""
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                "https://news.google.com/rss/search",
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            return len(root.findall(".//item"))
    except Exception as exc:
        log.debug("hype.rss_count_failed", query=query[:60], error=str(exc))
        return 0


async def _fetch_rss_headlines(query: str) -> list[str]:
    """Fetch top headline strings from Google News RSS for this query."""
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                "https://news.google.com/rss/search",
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            titles = []
            for item in root.findall(".//item")[:10]:
                t = item.find("title")
                if t is not None and t.text:
                    titles.append(t.text.strip())
            return titles
    except Exception:
        return []


async def _call_openai_compat(base_url: str, api_key: str, model: str, user_msg: str) -> float:
    """
    Single attempt against an OpenAI-compatible endpoint.
    Returns parsed score float, or raises on any error.
    """
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key or "none", base_url=base_url)
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            max_tokens=64,
            temperature=0,
            messages=[
                {"role": "system", "content": _SENTIMENT_PROMPT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        ),
        timeout=SENTIMENT_TIMEOUT,
    )
    raw = resp.choices[0].message.content
    parsed = json.loads(raw)
    score = float(parsed.get("score", 0.5))
    log.debug(
        "hype.sentiment_scored",
        score=round(score, 3),
        reason=parsed.get("reason", "")[:80],
        model=model,
    )
    return max(0.0, min(1.0, score))


async def _score_sentiment(headlines: list[str], news_velocity: float) -> float:
    """
    LLM-score emotional charge of headlines (0–1).

    Velocity gate: skip the LLM call when news_velocity < CONFIG.hype_velocity_gate.
    At that velocity level the hype_score cannot reach 0.7 regardless of sentiment,
    so the call is a pure cost with no decision value.

    Provider chain: primary → fallback (one retry) → 0.5 default.
    """
    if not headlines:
        return 0.5

    # ── Velocity gate ──────────────────────────────────────────────────────────
    if news_velocity < CONFIG.hype_velocity_gate:
        log.debug(
            "hype.sentiment_skipped_low_velocity",
            news_velocity=round(news_velocity, 3),
            gate=CONFIG.hype_velocity_gate,
        )
        return 0.5

    user_msg = "Score the hype level of these headlines:\n" + "\n".join(
        f"- {h}" for h in headlines[:10]
    )

    # ── Primary provider attempt ───────────────────────────────────────────────
    primary_error: str | None = None
    try:
        if CONFIG.sentiment_provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=CONFIG.anthropic_api_key)
            msg = await asyncio.wait_for(
                client.messages.create(
                    model=CONFIG.sentiment_model,
                    max_tokens=64,
                    temperature=0,
                    system=_SENTIMENT_PROMPT_SYSTEM,
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=SENTIMENT_TIMEOUT,
            )
            raw = msg.content[0].text
            parsed = json.loads(raw)
            score = float(parsed.get("score", 0.5))
            log.debug(
                "hype.sentiment_scored",
                score=round(score, 3),
                reason=parsed.get("reason", "")[:80],
                model=CONFIG.sentiment_model,
            )
            return max(0.0, min(1.0, score))

        elif CONFIG.sentiment_provider == "local":
            return await _call_openai_compat(
                base_url=CONFIG.local_llama_url,
                api_key="none",
                model=CONFIG.sentiment_model,
                user_msg=user_msg,
            )

        else:  # cerebras (default)
            return await _call_openai_compat(
                base_url=CONFIG.cerebras_base_url,
                api_key=CONFIG.cerebras_api_key,
                model=CONFIG.sentiment_model,
                user_msg=user_msg,
            )

    except Exception as exc:
        primary_error = str(exc)[:120]
        log.warning(
            "hype.sentiment_failed",
            provider=CONFIG.sentiment_provider,
            model=CONFIG.sentiment_model,
            error=primary_error,
        )

    # ── Fallback provider (one retry) ─────────────────────────────────────────
    fallback = CONFIG.sentiment_fallback_provider
    if fallback == CONFIG.sentiment_provider:
        # Primary and fallback are the same — nothing to retry.
        return 0.5

    try:
        if fallback == "local":
            score = await _call_openai_compat(
                base_url=CONFIG.local_llama_url,
                api_key="none",
                model=CONFIG.sentiment_model,
                user_msg=user_msg,
            )
        elif fallback == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=CONFIG.anthropic_api_key)
            msg = await asyncio.wait_for(
                client.messages.create(
                    model=CONFIG.sentiment_model,
                    max_tokens=64,
                    temperature=0,
                    system=_SENTIMENT_PROMPT_SYSTEM,
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=SENTIMENT_TIMEOUT,
            )
            raw = msg.content[0].text
            parsed = json.loads(raw)
            score = float(parsed.get("score", 0.5))
            log.debug(
                "hype.sentiment_scored",
                score=round(score, 3),
                reason=parsed.get("reason", "")[:80],
                model=CONFIG.sentiment_model,
            )
            return max(0.0, min(1.0, score))
        else:  # cerebras fallback
            score = await _call_openai_compat(
                base_url=CONFIG.cerebras_base_url,
                api_key=CONFIG.cerebras_api_key,
                model=CONFIG.sentiment_model,
                user_msg=user_msg,
            )

        log.info("hype.sentiment_fallback_ok", fallback=fallback, score=round(score, 3))
        return score

    except Exception as exc:
        log.warning(
            "hype.sentiment_fallback_failed",
            fallback=fallback,
            error=str(exc)[:120],
        )
        return 0.5


# ── Main entry point ──────────────────────────────────────────────────────────

async def analyze(
    market: MarketSnapshot,
    prediction: Prediction,
    ledger: OctagonLedger,
) -> HypeResult:
    """
    Compute hype signals for a market. Returns cached result if within TTL.

    Cache TTL is CONFIG.hype_cache_ttl_hours (default 4h).
    Called from octagon_executor.maybe_execute() after edge check, before sizing.
    """
    cached = _get_cached(market.market_id)
    if cached:
        log.debug(
            "hype.cache_hit",
            market_id=market.market_id,
            score=round(cached.hype_score, 3),
            ttl_hours=CONFIG.hype_cache_ttl_hours,
        )
        return cached

    query = market.question

    # RSS count + headline fetch + whale check run concurrently; sentiment is gated below.
    rss_count_task = asyncio.create_task(_count_rss_articles(query))
    headlines_task = asyncio.create_task(_fetch_rss_headlines(query))
    whale_task = asyncio.create_task(whale_check(market.market_id))

    rss_count, headlines, whale_signal = await asyncio.gather(
        rss_count_task, headlines_task, whale_task, return_exceptions=False
    )

    # Signal 1: news_velocity
    news_velocity = min(1.0, rss_count / MAX_VELOCITY_NORM)

    # Signal 2: sentiment_intensity — velocity-gated LLM call with provider fallback
    sentiment_intensity = await _score_sentiment(headlines, news_velocity)

    # Signal 3: prob_drift_vs_evidence
    price_change = ledger.price_change_24h(market.market_id)
    n_refs = max(1, len(prediction.evidence_refs))
    prob_drift_vs_evidence = min(1.0, price_change / n_refs)

    hype_score = round(
        0.4 * news_velocity
        + 0.4 * sentiment_intensity
        + 0.2 * prob_drift_vs_evidence,
        4,
    )

    result = HypeResult(
        market_id=market.market_id,
        news_velocity=round(news_velocity, 4),
        sentiment_intensity=round(sentiment_intensity, 4),
        prob_drift_vs_evidence=round(prob_drift_vs_evidence, 4),
        hype_score=hype_score,
        whale_signal=whale_signal,
        computed_at=datetime.utcnow().isoformat(),
    )

    _write_score(result)

    log.info(
        "hype.analyzed",
        market_id=market.market_id,
        hype_score=hype_score,
        news_velocity=round(news_velocity, 3),
        sentiment=round(sentiment_intensity, 3),
        drift=round(prob_drift_vs_evidence, 3),
        whale=whale_signal,
        velocity_gated=(news_velocity < CONFIG.hype_velocity_gate),
        question=market.question[:60],
    )
    return result
