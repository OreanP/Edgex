from tools.manifold import (
    get_markets,
    scout_markets,
    to_market
)

from agent.agent import (
    analyse_market,
    AgentError
)

<<<<<<< HEAD
=======
from agent.critic import (CriticResult)

>>>>>>> main

print(
    "Fetching Manifold markets..."
)


raw_markets = get_markets(
    limit=20,
    topic="ai"
)


candidates = scout_markets(
    raw_markets,
    min_volume_24h=50
)


if not candidates:

    raise RuntimeError(
        "No suitable Manifold market found."
    )


market = to_market(
    candidates[0]
)


print(
    "\nSelected market:"
)

print(
    market.question
)

print(
    "Manifold probability:",
    f"{market.probability:.1%}"
)


print(
    "\nRunning EdgeX..."
)


try:

    analysis = analyse_market(
        market
    )

except AgentError as e:

    raise RuntimeError(
        f"EdgeX failed: {e}"
    ) from e


print(
    "\n=========================="
)

print(
    "EDGEX RESULT"
)

print(
    "=========================="
)


print(
    "Market:",
    f"{analysis.market_probability:.1%}"
)

print(
    "Initial:",
    f"{analysis.initial_probability:.1%}"
)

print(
    "Final:",
    f"{analysis.final_probability:.1%}"
)

print(
    "Revision:",
    f"{analysis.final_probability - analysis.initial_probability:+.1%}"
)

print(
    "Edge:",
    f"{analysis.edge:+.1%}"
)

print(
    "Confidence:",
    analysis.confidence
)

print(
    "Decision:",
    analysis.decision
)


print(
    "\n=========================="
)

print(
    "RESEARCHER EVIDENCE"
)

print(
    "=========================="
)


for evidence in analysis.evidence:

    if (
        evidence.source_agent
        == "researcher"
    ):

        print(
            f"\n{'FOR' if evidence.supports else 'AGAINST'}"
        )

        print(
            evidence.title
        )

        print(
            evidence.summary
        )

        print(
            evidence.url
        )


print(
    "\n=========================="
)

print(
    "CRITIC EVIDENCE"
)

print(
    "=========================="
)


for evidence in analysis.evidence:

    if (
        evidence.source_agent
        == "critic"
    ):

        print(
            f"\n{'FOR' if evidence.supports else 'AGAINST'}"
        )

        print(
            evidence.title
        )

        print(
            evidence.summary
        )

        print(
            evidence.url
        )


print(
    "\n=========================="
)

print(
    "FINAL REASONING"
)

print(
    "=========================="
)

print(
    analysis.reasoning
)