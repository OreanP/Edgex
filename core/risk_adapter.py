from pydantic import BaseModel
from typing import Literal

from agent.schemas import Market, AgentAnalysis


class SimpleRiskDecision(BaseModel):
    approved: bool
    reason: str

    outcome: Literal[
        "YES",
        "NO"
    ] | None = None

    amount: float = 0.0


def evaluate_analysis(
    market: Market,
    analysis: AgentAnalysis,
    max_bet_mana: float = 10.0,
    min_edge: float = 0.08
) -> SimpleRiskDecision:

    # =========================
    # MANA ONLY
    # =========================

    if market.token != "MANA":
        return SimpleRiskDecision(
            approved=False,
            reason="NON_MANA_MARKET"
        )

    # =========================
    # AGENT SKIPPED
    # =========================

    if analysis.decision == "SKIP":
        return SimpleRiskDecision(
            approved=False,
            reason="AGENT_SKIP"
        )

    # =========================
    # CONFIDENCE
    # =========================

    if analysis.confidence == "low":
        return SimpleRiskDecision(
            approved=False,
            reason="LOW_CONFIDENCE"
        )

    # =========================
    # EDGE
    # =========================

    if abs(analysis.edge) < min_edge:
        return SimpleRiskDecision(
            approved=False,
            reason="EDGE_TOO_SMALL"
        )

    # =========================
    # OUTCOME
    # =========================

    if analysis.decision == "BUY_YES":
        outcome = "YES"

    elif analysis.decision == "BUY_NO":
        outcome = "NO"

    else:
        return SimpleRiskDecision(
            approved=False,
            reason="INVALID_DECISION"
        )

    # =========================
    # POSITION SIZE
    # =========================

    # Simple MVP sizing:
    # 10 Mana max, scaled by edge.

    amount = min(
        max_bet_mana,
        max(
            1.0,
            abs(analysis.edge) * 100
        )
    )

    return SimpleRiskDecision(
        approved=True,
        reason="APPROVED",
        outcome=outcome,
        amount=round(amount, 2)
    )