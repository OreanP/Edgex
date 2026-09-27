from agent.schemas import Market
from agent.agent import analyse_market

market = Market(
    id="test-002",
    question="Will Bitcoin reach $150,000 before December 31, 2026?",
    probability=0.42,
    description=(
        "Resolves YES if the BTC/USD price reaches "
        "$150,000 on a recognized major exchange "
        "before December 31, 2026."
    )
)

analysis = analyse_market(market)

print("\n=== EDGEX ANALYSIS ===")

print(
    f"Market probability: "
    f"{analysis.market_probability:.1%}"
)

print(
    f"Agent probability: "
    f"{analysis.final_probability:.1%}"
)

print("\nEvidence FOR:")

for evidence in analysis.evidence:
    if evidence.supports:
        print("+", evidence.summary)

print("\nEvidence AGAINST:")

for evidence in analysis.evidence:
    if not evidence.supports:
        print("-", evidence.summary)

print(
    f"Confidence: "
    f"{analysis.confidence}"
)

print(
    f"Decision: "
    f"{analysis.decision}"
)

print(
    f"Reasoning: "
    f"{analysis.reasoning}"
)