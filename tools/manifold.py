import time
import os

from dotenv import load_dotenv


load_dotenv()

BASE_URL = "https://api.manifold.markets/v0"

API_KEY = os.getenv("MANIFOLD_API_KEY")

BASE_URL="https://api.manifold.markets/v0"

def get_headers():
    return {
        "Authorization": f"Key {API_KEY}"
    }

def get_markets(limit=5, topic="ai"):
    
    
    params = {
        "filter": "open",
        "contractType": "BINARY",
        "topicSlug": topic,
        "sort": "24-hour-vol",
        "limit": limit
    }

    response = requests.get(
        f"{BASE_URL}/search-markets",
        params=params
    )

    response.raise_for_status()

    return response.json()

def get_market(market_id):
    response = requests.get(
        f"{BASE_URL}/market/{market_id}"
    )

    response.raise_for_status()

    return response.json()

def scout_markets(markets, min_volume_24h=50):
    candidates = []

    for market in markets:
        probability = market.get("probability")
        volume_24h = market.get("volume24Hours", 0)

        # Une probabilité est indispensable pour A
        if probability is None:
            continue

        # Sécurité : uniquement marchés binaires
        if market.get("outcomeType") != "BINARY":
            continue

        # Sécurité : pas de marché déjà résolu
        if market.get("isResolved", False):
            continue

        # Élimine les marchés sans activité récente suffisante
        if volume_24h < min_volume_24h:
            continue

        candidates.append(market)

    return candidates

<<<<<<< HEAD
if __name__ == "__main__":
    markets = get_markets(limit=50, topic="ai")

    
=======

markets = get_markets(
    limit=50,
    topic="ai"
)
>>>>>>> main

def to_market(manifold_market):
    return Market(
        id=manifold_market.get("id"),
        question=manifold_market.get("question"),
        probability=manifold_market.get("probability"),
        description=manifold_market.get("description"),
        url=manifold_market.get("url"),
        volume = manifold_market.get("volume24Hours"),
        close_time=manifold_market.get("closeTime"),
        fetched_at= time.time(),
        token= manifold_market.get("token","MANA")
    )

def get_me():
    response = requests.get(
        f"{BASE_URL}/me",
        headers=get_headers()
    )

    response.raise_for_status()

    return response.json()

def place_bet(market_id, outcome, amount, dry_run=True):
    if outcome not in ("YES", "NO"):
        raise ValueError("outcome doit être 'YES' ou 'NO'")

    if amount <= 0:
        raise ValueError("amount doit être supérieur à 0")

    payload = {
        "amount": amount,
        "contractId": market_id,
        "outcome": outcome,
        "dryRun": dry_run
    }

    response = requests.post(
        f"{BASE_URL}/bet",
        headers=get_headers(),
        json=payload
    )

    response.raise_for_status()

    return response.json()


if __name__ == "__main__":
    markets = get_markets(limit=50, topic="ai")

    result = place_bet(
        market_id="A319ydGB1B7f4PMOROL3",
        outcome="YES",
        amount=1,
        dry_run=True
    )

    print(result)

