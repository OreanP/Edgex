import os
import time

import requests
from dotenv import load_dotenv

from agent.schemas import Market


load_dotenv()


BASE_URL = "https://api.manifold.markets/v0"
API_KEY = os.getenv("MANIFOLD_API_KEY")


def get_headers():
    if not API_KEY:
        raise RuntimeError(
            "MANIFOLD_API_KEY is missing."
        )

    return {
        "Authorization": f"Key {API_KEY}"
    }


def get_markets(
    limit=5,
    topic="ai"
):
    params = {
        "filter": "open",
        "contractType": "BINARY",
        "topicSlug": topic,
        "sort": "24-hour-vol",
        "limit": limit
    }

    response = requests.get(
        f"{BASE_URL}/search-markets",
        params=params,
        timeout=10
    )

    response.raise_for_status()

    return response.json()


def get_market(
    market_id: str
):
    response = requests.get(
        f"{BASE_URL}/market/{market_id}",
        timeout=10
    )

    response.raise_for_status()

    return response.json()


def scout_markets(
    markets,
    min_volume_24h=50
):
    candidates = []

    for market in markets:

        probability = market.get(
            "probability"
        )

        volume_24h = market.get(
            "volume24Hours",
            0
        )

        # Probability required by agent A
        if probability is None:
            continue

        # Binary markets only
        if market.get(
            "outcomeType"
        ) != "BINARY":
            continue

        # Open markets only
        if market.get(
            "isResolved",
            False
        ):
            continue

        # Require recent activity
        if volume_24h < min_volume_24h:
            continue

        # Mana only
        if market.get(
            "token",
            "MANA"
        ) != "MANA":
            continue

        candidates.append(
            market
        )

    return candidates


def to_market(
    manifold_market
):
    return Market(
        id=manifold_market["id"],

        question=(
            manifold_market["question"]
        ),

        probability=(
            manifold_market["probability"]
        ),

        description=(
            manifold_market.get(
                "description"
            )
        ),

        url=(
            manifold_market.get(
                "url"
            )
        ),

        volume=(
            manifold_market.get(
                "volume24Hours"
            )
        ),

        close_time=(
            manifold_market.get(
                "closeTime"
            )
        ),

        fetched_at=time.time(),

        token=(
            manifold_market.get(
                "token",
                "MANA"
            )
        )
    )


def get_me():
    response = requests.get(
        f"{BASE_URL}/me",
        headers=get_headers(),
        timeout=10
    )

    response.raise_for_status()

    return response.json()


def place_bet(
    market_id: str,
    outcome: str,
    amount: float,
    dry_run=True
):
    if outcome not in (
        "YES",
        "NO"
    ):
        raise ValueError(
            "outcome doit être YES ou NO"
        )

    if amount <= 0:
        raise ValueError(
            "amount doit être supérieur à 0"
        )

    payload = {
        "amount": amount,
        "contractId": market_id,
        "outcome": outcome,
        "dryRun": dry_run
    }

    response = requests.post(
        f"{BASE_URL}/bet",
        headers=get_headers(),
        json=payload,
        timeout=10
    )

    response.raise_for_status()

    return response.json()