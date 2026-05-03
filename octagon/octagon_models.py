from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
import uuid


@dataclass
class MarketSnapshot:
    market_id: str
    question: str
    yes_price: float          # midpoint 0.0–1.0
    bid_yes: float
    ask_yes: float
    depth_yes_usd: float      # USD depth within config.depth_cents of mid
    depth_no_usd: float
    volume_24h: float
    resolves_at: datetime
    resolution_criteria: str
    category: str
    snapshot_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class SourceDoc:
    url: str
    fetched_at: datetime
    content: str
    source_class: Literal[
        "primary_news", "official_gov", "official_company", "primary_social", "other"
    ]


@dataclass
class Prediction:
    prediction_id: str
    market_id: str
    p_yes: float               # calibration-adjusted estimate
    p_yes_raw: float           # before calibration adjustment
    confidence: float          # 0.0–1.0
    edge: float                # p_yes - market_price_at_prediction
    reasoning: str             # full trace (also written to disk)
    evidence_refs: list[str]   # source URLs cited
    market_price_at_prediction: float
    resolution_clarity: float  # 0.0–1.0, collapses confidence on ambiguity
    unciteable: bool           # True if any claim lacked a citable source
    predicted_at: datetime
    ttl_seconds: int
    base_rate: float
    base_rate_ref_class: str
    base_rate_source: str
    adjustments: list[dict]         # [{direction, magnitude, source, reasoning}]
    edge_cases_considered: list[dict]  # [{description, severity}]

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())


@dataclass
class Trade:
    trade_id: str
    prediction_id: str
    market_id: str
    side: Literal["YES", "NO"]
    entry_price: float
    size_usd: float
    entered_at: datetime


@dataclass
class Resolution:
    market_id: str
    outcome: Literal["YES", "NO", "INVALID"]
    resolved_at: datetime
    source: str = ""
