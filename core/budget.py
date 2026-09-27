import json
import os
from datetime import datetime, timezone
from pathlib import Path


USAGE_FILE = Path("data/usage.json")

MAX_ANALYSES_PER_RUN = int(
    os.getenv("MAX_ANALYSES_PER_RUN", "3")
)

MAX_ANALYSES_PER_DAY = int(
    os.getenv("MAX_ANALYSES_PER_DAY", "20")
)


class BudgetExceeded(Exception):
    pass


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


"""def _load_usage() -> dict:
    if not USAGE_FILE.exists():
        return {
            "date": _today(),
            "analyses": 0
        }

    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("date") != _today():
        return {
            "date": _today(),
            "analyses": 0
        }

    return data"""



def _load_usage() -> dict:
    default_usage = {
        "date": _today(),
        "analyses": 0
    }

    if not USAGE_FILE.exists():
        return default_usage

    try:
        with open(
            USAGE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

    except (
        json.JSONDecodeError,
        OSError
    ):
        return default_usage

    if data.get("date") != _today():
        return default_usage

    return data

def _save_usage(data: dict) -> None:
    USAGE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        USAGE_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            indent=2
        )


def check_daily_budget() -> None:
    usage = _load_usage()

    if usage["analyses"] >= MAX_ANALYSES_PER_DAY:
        raise BudgetExceeded(
            "Daily OpenAI analysis budget reached."
        )


def register_analysis() -> None:
    usage = _load_usage()

    usage["analyses"] += 1

    _save_usage(usage)


def remaining_analyses() -> int:
    usage = _load_usage()

    return max(
        MAX_ANALYSES_PER_DAY
        - usage["analyses"],
        0
    )