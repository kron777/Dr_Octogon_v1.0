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
class Adjustment:
    direction: Literal["up", "down"]
    magnitude_pp: float   # magnitude in percentage points (5 = 5pp = +0.05 probability)
    source_url: str
    rationale: str


@dataclass
class Prediction:
    prediction_id: str
    market_id: str
    p_yes: float                     # calibration-adjusted estimate
    p_yes_raw: float                 # before calibration adjustment
    base_rate: float
    base_rate_reference_class: str
    adjustments: list[Adjustment]
    confidence: float                # 0.0–1.0
    edge: float                      # p_yes - market_price_at_prediction
    reasoning_trace_path: str        # path to JSON trace file on disk
    evidence_refs: list[str]         # source URLs fetched
    market_price_at_prediction: float
    resolution_clarity: float        # 0.0–1.0
    edge_cases: list[str]
    unciteable: bool
    predicted_at: datetime
    ttl_seconds: int
    model_used: str = ""

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
