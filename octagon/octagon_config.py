import os
from dataclasses import dataclass, field


def _env_list(key: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(key, default).split(",") if s.strip()]


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

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    research_model: str = os.getenv("RESEARCH_MODEL", "claude-sonnet-4-6")
    criteria_model: str = os.getenv("CRITERIA_MODEL", "claude-haiku-4-5-20251001")

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


CONFIG = OctagonConfig()
