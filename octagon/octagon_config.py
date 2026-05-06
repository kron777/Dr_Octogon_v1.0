import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root (one level above this file's package directory)
load_dotenv(Path(__file__).parent.parent / ".env")

FREQ_LEVER_PATH = Path.home() / "Desktop" / "octagon" / "freq_lever.json"


def _env_list(key: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(key, default).split(",") if s.strip()]


@dataclass(frozen=True)
class Tier:
    name: str
    min_bankroll: float
    max_stake_usd: float
    min_edge_to_trade: float
    max_edge_to_trade: float  # reject implausibly large edges (forecaster overconfidence)
    daily_loss_cap_usd: float
    kelly_fraction: float


TIERS: list[Tier] = [
    Tier("P01_Hibernate",    0.0,    0.25,  0.20,  0.30,   1.00,  0.20),
    Tier("P02_Cautious",     0.0,    0.50,  0.15,  0.25,   2.00,  0.25),
    Tier("P03_Default",     10.0,    0.50,  0.10,  0.20,   2.00,  0.25),
    Tier("P04_Active",      25.0,    1.00,  0.07,  0.18,   5.00,  0.30),
    Tier("P05_Engaged",    100.0,    2.00,  0.05,  0.15,  10.00,  0.32),
    Tier("P06_Aggressive", 250.0,    5.00,  0.04,  0.13,  25.00,  0.35),
    Tier("P07_Hot",        500.0,   10.00,  0.03,  0.12,  50.00,  0.38),
    Tier("P08_Burning",   1000.0,   25.00,  0.025, 0.10, 125.00,  0.40),
    Tier("P09_Furnace",   2500.0,   50.00,  0.02,  0.10, 250.00,  0.42),
    Tier("P10_Nuclear",   5000.0,  100.00,  0.015, 0.08, 500.00,  0.45),
]


@dataclass
class OctagonConfig:
    loop_interval_seconds: int = int(os.getenv("LOOP_INTERVAL_SECONDS", "900"))
    min_depth_usd: float = float(os.getenv("MIN_DEPTH_USD", "500"))
    max_spread: float = float(os.getenv("MAX_SPREAD", "0.04"))
    min_hours_to_resolution: float = float(os.getenv("MIN_HOURS_TO_RESOLUTION", "4"))
    max_hours_to_resolution: float = float(os.getenv("MAX_HOURS_TO_RESOLUTION", "336"))
    depth_cents: float = float(os.getenv("DEPTH_CENTS", "3"))
    repredict_threshold: float = float(os.getenv("REPREDICT_THRESHOLD", "0.03"))
    ttl_seconds: int = int(os.getenv("TTL_SECONDS", "3600"))
    max_markets_per_scan: int = int(os.getenv("MAX_MARKETS_PER_SCAN", "500"))
    research_spacing_seconds: int = int(os.getenv("RESEARCH_SPACING_SECONDS", "8"))

    allowed_categories: list[str] = field(
        default_factory=lambda: _env_list("ALLOWED_CATEGORIES", "Politics,Macro,Crypto,Economics,Finance")
    )

    db_path: str = os.path.expanduser(
        os.getenv("DB_PATH", "~/Desktop/octagon/octagon.db")
    )
    trace_dir: str = os.path.expanduser(
        os.getenv("TRACE_DIR", "~/Desktop/octagon/traces")
    )
    log_path: str = os.path.expanduser(
        os.getenv("LOG_PATH", "~/Desktop/octagon/logs/octagon.log")
    )

    # ── Hype-fade lane ────────────────────────────────────────────────────────
    hype_fade_enabled: bool = os.getenv("HYPE_FADE_ENABLED", "false").lower() == "true"
    hype_fade_min_score: float = float(os.getenv("HYPE_FADE_MIN_SCORE", "0.7"))
    hype_fade_min_edge: float = float(os.getenv("HYPE_FADE_MIN_EDGE", "0.15"))
    hype_fade_daily_cap: int = int(os.getenv("HYPE_FADE_DAILY_CAP", "5"))
    # Velocity gate: skip sentiment LLM call when news_velocity < this threshold.
    # At velocity < 0.5, hype_score is capped at 0.4*0.5 + 0.4*1.0 + 0.2*1.0 = 0.8 max, but
    # realistically stays below 0.7 without both velocity and sentiment firing.
    hype_velocity_gate: float = float(os.getenv("HYPE_VELOCITY_GATE", "0.5"))
    # Cache TTL in hours (was hardcoded to 1h; longer TTL reduces LLM costs for slow-moving markets).
    hype_cache_ttl_hours: float = float(os.getenv("HYPE_CACHE_TTL_HOURS", "4"))
    # Sentiment model for hype detector. Spec wants gpt-oss-120b but it returns 404 on
    # the current free-tier Cerebras key. Options: llama3.1-8b (fast/weak),
    # claude-haiku-4-5-20251001 (strong, set SENTIMENT_PROVIDER=anthropic),
    # or qwen-3-235b-a22b-instruct-2507 (available until ~2026-05-27 deprecation).
    # SENTIMENT_PROVIDER=local uses LOCAL_LLAMA_URL (OpenAI-compatible, no key required).
    sentiment_model: str = os.getenv("SENTIMENT_MODEL", "llama3.1-8b")
    sentiment_provider: str = os.getenv("SENTIMENT_PROVIDER", "cerebras")
    # Fallback provider when primary errors or 429s. "local" uses LOCAL_LLAMA_URL.
    sentiment_fallback_provider: str = os.getenv("SENTIMENT_FALLBACK_PROVIDER", "local")
    local_llama_url: str = os.getenv("LOCAL_LLAMA_URL", "http://localhost:8080/v1")
    hype_score_cache_path: str = os.path.expanduser(
        os.getenv("HYPE_SCORE_CACHE_PATH", "~/Desktop/octagon/hype_scores.json")
    )

    # ── Copy-trading lane ─────────────────────────────────────────────────────
    copy_trade_enabled: bool = os.getenv("COPY_TRADE_ENABLED", "false").lower() == "true"
    copy_trade_ratio: float = float(os.getenv("COPY_TRADE_RATIO", "0.1"))
    copy_trade_max_wallets: int = int(os.getenv("COPY_TRADE_MAX_WALLETS", "5"))
    copy_trade_min_win_rate: float = float(os.getenv("COPY_TRADE_MIN_WIN_RATE", "0.70"))
    copy_trade_min_resolved: int = int(os.getenv("COPY_TRADE_MIN_RESOLVED", "100"))
    copy_trade_min_median_usd: float = float(os.getenv("COPY_TRADE_MIN_MEDIAN_USD", "10.0"))
    copy_trade_daily_loss_cap_pct: float = float(os.getenv("COPY_TRADE_DAILY_LOSS_CAP_PCT", "0.05"))
    copy_trade_drawdown_stop_pct: float = float(os.getenv("COPY_TRADE_DRAWDOWN_STOP_PCT", "0.15"))
    copy_trade_poll_interval_s: float = float(os.getenv("COPY_TRADE_POLL_INTERVAL_S", "5.0"))
    copy_trade_wallet_refresh_s: int = int(os.getenv("COPY_TRADE_WALLET_REFRESH_S", "1800"))
    copy_trade_hud_path: str = os.path.expanduser(
        os.getenv("COPY_TRADE_HUD_PATH", "~/Desktop/octagon/copy_trade_state.json")
    )

    # ── Executor (legacy flat fields — superseded by tier system) ─────────────
    live_trading_enabled: bool = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
    max_stake_usd: float = float(os.getenv("MAX_STAKE_USD", "0.50"))
    daily_loss_cap_usd: float = float(os.getenv("DAILY_LOSS_CAP_USD", "2.00"))
    min_edge_to_trade: float = float(os.getenv("MIN_EDGE_TO_TRADE", "0.10"))
    kelly_fraction: float = float(os.getenv("KELLY_FRACTION", "0.25"))
    starting_bankroll_usd: float = float(os.getenv("STARTING_BANKROLL_USD", "10.00"))

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    criteria_provider: str = os.getenv("CRITERIA_PROVIDER", "cerebras")
    criteria_model: str = os.getenv("CRITERIA_MODEL", "llama3.1-8b")
    # Swap-back: set CRITERIA_PROVIDER=anthropic and CRITERIA_MODEL to the value below
    # haiku_model = "claude-haiku-4-5-20251001"
    haiku_model: str = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")

    forecaster_provider: str = os.getenv("FORECASTER_PROVIDER", "cerebras")
    # Only qwen-3-235b-a22b-instruct-2507 and llama3.1-8b are accessible on this free-tier key
    # gpt-oss-120b and zai-glm-4.7 return 404 despite appearing in models.list()
    forecaster_model: str = os.getenv("FORECASTER_MODEL", "qwen-3-235b-a22b-instruct-2507")
    cerebras_api_key: str = os.getenv("CEREBRAS_API_KEY", "")
    cerebras_base_url: str = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")

    # Swap-back: set FORECASTER_PROVIDER=anthropic and FORECASTER_MODEL to the value below
    # opus_model = "claude-opus-4-7"
    opus_model: str = os.getenv("OPUS_MODEL", "claude-opus-4-7")

    # Primary news, official government, financial data sources
    source_whitelist_domains: list[str] = field(default_factory=lambda: [
        "apnews.com",
        "reuters.com",
        "bloomberg.com",
        "ft.com",
        "wsj.com",
        "nytimes.com",
        "washingtonpost.com",
        "bbc.com",
        "bbc.co.uk",
        "theguardian.com",
        "economist.com",
        "nature.com",
        "science.org",
        "sciencemag.org",
        "federalreserve.gov",
        "bls.gov",
        "census.gov",
        "sec.gov",
        "treasury.gov",
        "whitehouse.gov",
        "congress.gov",
        "ecb.europa.eu",
        "imf.org",
        "worldbank.org",
        "bis.org",
        "coindesk.com",
        "cointelegraph.com",
        "theblock.co",
        "axios.com",
        "politico.com",
        "thehill.com",
        "rollcall.com",
        "npr.org",
        "state.gov",
    ])

    # Prediction-market commentary, aggregators, insider accounts — hard-banned.
    # Evidence from these is contaminated by the price signal you are trying to beat.
    source_blacklist_domains: list[str] = field(default_factory=lambda: [
        "polymarket.com",
        "kalshi.com",
        "manifold.markets",
        "metaculus.com",
        "predictit.org",
        "electionbettingodds.com",
        "oddschecker.com",
        "predictionbook.com",
        "augur.net",
        "smarkets.com",
    ])

    def active_tier(self, bankroll: float) -> Tier:
        """Highest tier whose min_bankroll <= bankroll."""
        result = TIERS[0]
        for t in TIERS:
            if bankroll >= t.min_bankroll:
                result = t
        return result


CONFIG = OctagonConfig()


def get_effective_tier(bankroll: float) -> tuple[Tier, str]:
    """
    Returns (tier, mode). Reads freq_lever.json; falls back to auto on any error.
    mode is 'auto' or 'manual'. manual_position is 1-indexed.
    """
    try:
        if FREQ_LEVER_PATH.exists():
            data = json.loads(FREQ_LEVER_PATH.read_text())
            mode = data.get("mode", "auto")
            if mode == "manual":
                pos = int(data.get("manual_position", 3))
                pos = max(1, min(len(TIERS), pos))
                return TIERS[pos - 1], "manual"
    except Exception:
        pass
    return CONFIG.active_tier(bankroll), "auto"
