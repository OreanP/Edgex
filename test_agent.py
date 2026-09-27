from agent.schemas import Market
from agent.agent import analyse_market



"""market = Market(
=======
market = Market(
>>>>>>> market-data
    id="test-002",
    question="Will Bitcoin reach $150,000 before December 31, 2026?",
    probability=0.42,
    description=(
        "Resolves YES if the BTC/USD price reaches "
        "$150,000 on a recognized major exchange "
        "before December 31, 2026."
    )

)"""

market = Market(
    id="test-energy-001",
    question=(
        "Will global solar power capacity exceed "
        "3 terawatts before December 31, 2027?"
    ),
    probability=0.55,
    description=(
        "Resolves YES if authoritative international energy statistics "
        "report that total installed global solar photovoltaic capacity "
        "has exceeded 3 terawatts before December 31, 2027."
    )
)


print("\n==============================")
print("       EDGEX TEST")
print("==============================")

print("\nAnalyzing market...")
print(market.question)

analysis = analyse_market(market)


print("\n==============================")
print("       EDGEX ANALYSIS")
print("==============================")

print(
    f"\nMarket probability:  "

)

analysis = analyse_market(market)

print("\n=== EDGEX ANALYSIS ===")

print(
    f"Market probability: "

    f"{analysis.market_probability:.1%}"
)

print(

    f"Initial probability: "
    f"{analysis.initial_probability:.1%}"
)

print(
    f"Final probability:   "
    f"{analysis.final_probability:.1%}"
)

print(
    f"Revision:            "
    f"{analysis.final_probability - analysis.initial_probability:+.1%}"
)

print(
    f"Edge vs market:      "
    f"{analysis.edge:+.1%}"
)

print(
    f"Confidence:          "
    f"{analysis.confidence}"
)

print(
    f"Decision:            "
    f"{analysis.decision}"
)


print("\n==============================")
print("       EVIDENCE FOR")
print("==============================")

for evidence in analysis.evidence:
    if evidence.supports:

        print(f"\n+ {evidence.title}")
        print(f"  {evidence.summary}")
        print(f"  Source: {evidence.url}")

        # Si tu as ajouté source_agent dans Evidence
        if hasattr(evidence, "source_agent"):
            print(
                f"  Found by: "
                f"{evidence.source_agent}"
            )


print("\n==============================")
print("     EVIDENCE AGAINST")
print("==============================")

for evidence in analysis.evidence:
    if not evidence.supports:

        print(f"\n- {evidence.title}")
        print(f"  {evidence.summary}")
        print(f"  Source: {evidence.url}")

        if hasattr(evidence, "source_agent"):
            print(
                f"  Found by: "
                f"{evidence.source_agent}"
            )


print("\n==============================")
print("          REASONING")
print("==============================")

print(analysis.reasoning)


print("\n==============================")
print("           RESULT")
print("==============================")

print(
    f"""
Manifold market : {analysis.market_probability:.1%}
EdgeX initial   : {analysis.initial_probability:.1%}
EdgeX final     : {analysis.final_probability:.1%}

Revision        : {analysis.final_probability - analysis.initial_probability:+.1%}
Edge            : {analysis.edge:+.1%}

Confidence      : {analysis.confidence}
Decision        : {analysis.decision}
"""
)











































"""print("\nEvidence FOR:")
=======
    f"Agent probability: "
    f"{analysis.final_probability:.1%}"
)

print("\nEvidence FOR:")
>>>>>>> market-data

for evidence in analysis.evidence:
    if evidence.supports:
        print("+", evidence.summary)

print("\nEvidence AGAINST:")

for evidence in analysis.evidence:
    if not evidence.supports:
<<<<<<< HEAD
        print("-", evidence.summary)"""

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

