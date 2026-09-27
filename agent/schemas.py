from pydantic import BaseModel, Field
from typing import Literal

class Market(BaseModel):
    id: str
    question: str
    probability: float
    description: str | None = None
    url: str | None = None
    volume: float | None = None
    close_time: float | None = None
    fetched_at: float | None = None
    token: str = "MANA"


class Evidence(BaseModel):
    title: str
    url: str
    summary: str
    supports: bool
    source_agent: Literal["researcher","critic"]


class AgentAnalysis(BaseModel):
    market_probability: float

    initial_probability: float
    final_probability: float

    edge: float

    confidence: Literal["low", "medium", "high"]

    evidence: list[Evidence]

    decision: Literal[
        "BUY_YES",
        "BUY_NO",
        "SKIP"
    ]

    reasoning: str

    

class Forecast(BaseModel):
    probability: float = Field(ge=0, le=1)
    confidence: Literal["low","medium","high"]
    reasoning: str
    evidence:list[Evidence]

class CriticResult(BaseModel):
    revised_probability: float = Field(ge=0,le=1)

    confidence: Literal[
        "low",
        "medium",
        "high"
    ]
    counter_evidence: list[Evidence]

    reasoning: str


