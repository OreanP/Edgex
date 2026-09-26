from pydantic import BaseModel, Field
from typing import Literal

class Market(BaseModel):
    id: str
    question: str
    probability: float
    description: str | None = None


class Evidence(BaseModel):
    title: str
    url: str
    summary: str
    supports: bool


class AgentAnalysis(BaseModel):
    market_probability: float

    initial_probability: float
    final_probability: float

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