"""Small, explicit contracts for the portfolio preview (all times in seconds)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
from typing import Literal
from urllib.parse import urlsplit, urlunsplit


class InvalidData(ValueError):
    pass


def number(value, name: str, low: float = 0, high: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidData(f"{name}: nombre requis")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise InvalidData(f"{name}: valeur hors limites")
    return value


def text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidData(f"{name}: texte requis")
    return value.strip()


def canonical_url(value: str) -> str:
    try:
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.netloc or u.username:
            return ''
        return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or '/', u.query, ''))
    except (ValueError, TypeError):
        return ''


@dataclass(frozen=True)
class Answer:
    id: str
    label: str
    probability: float

    def __post_init__(self):
        text(self.id, 'answer.id')
        text(self.label, 'answer.label')
        number(self.probability, 'probability', 0, 1)


@dataclass(frozen=True)
class Market:
    id: str
    question: str
    description: str
    domain: str
    family: str
    kind: Literal['BINARY', 'SUM_TO_ONE', 'INDEPENDENT']
    answers: tuple[Answer, ...]
    close_time: float
    fetched_at: float
    volume_24h: float = 0
    url: str = ''

    def __post_init__(self):
        for key in ('id', 'question', 'description', 'domain', 'family'):
            text(getattr(self, key), key)
        if self.kind not in ('BINARY', 'SUM_TO_ONE', 'INDEPENDENT'):
            raise InvalidData('Type non supporté')
        if not 1 <= len(self.answers) <= 8:
            raise InvalidData('La V2 analyse 1 à 8 réponses, sans troncature')
        if len({a.id for a in self.answers}) != len(self.answers):
            raise InvalidData('Réponses dupliquées')
        if self.kind == 'BINARY' and tuple(a.id for a in self.answers) != ('YES',):
            raise InvalidData('Un marché binaire contient la probabilité YES')
        if self.kind == 'SUM_TO_ONE' and (len(self.answers) < 2 or abs(sum(a.probability for a in self.answers)-1) > .002):
            raise InvalidData('Probabilités du marché incohérentes (somme != 1)')
        number(self.fetched_at, 'fetched_at')
        number(self.close_time, 'close_time')
        number(self.volume_24h, 'volume_24h')
        if self.close_time <= self.fetched_at:
            raise InvalidData('Marché fermé')

    @property
    def conditions_hash(self) -> str:
        # Prices and observation time are NOT contract terms.
        terms = (self.id, self.question, self.description, self.kind, self.close_time,
                 [(a.id, a.label) for a in self.answers])
        return sha256(json.dumps(terms, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Evidence:
    title: str
    url: str
    summary: str
    source_agent: str


@dataclass(frozen=True)
class Analysis:
    market_id: str
    conditions_hash: str
    initial: dict[str, float]
    final: dict[str, float]
    confidence: str
    evidence: tuple[Evidence, ...]
    reasoning: str
    critic_completed: bool
    completed_at: float
    source: str = 'openai'

    def validate(self, market: Market) -> None:
        if self.market_id != market.id or self.conditions_hash != market.conditions_hash:
            raise InvalidData('Prévision pour un autre contrat')
        expected = {a.id for a in market.answers}
        for distribution in (self.initial, self.final):
            if set(distribution) != expected:
                raise InvalidData('IDs de réponses manquants/inconnus')
            for p in distribution.values():
                number(p, 'forecast.probability', 0, 1)
            if market.kind == 'SUM_TO_ONE' and abs(sum(distribution.values()) - 1) > .002:
                raise InvalidData('Prévision somme-à-un incohérente')
        if self.confidence not in ('low', 'medium', 'high'):
            raise InvalidData('Confiance invalide')
        number(self.completed_at, 'analysis.completed_at')

    @classmethod
    def from_dict(cls, data: dict) -> 'Analysis':
        data = dict(data)
        data['evidence'] = tuple(Evidence(**e) for e in data['evidence'])
        return cls(**data)


@dataclass(frozen=True)
class Quote:
    market_id: str
    answer_id: str
    side: str
    amount: float
    estimated_debit: float
    shares: float
    probability_before: float
    quoted_at: float
    source: str
    full_fill: bool
    # This is deliberately NOT an authorization or a guaranteed debit bound.


@dataclass(frozen=True)
class Option:
    id: str
    market_id: str
    question: str
    answer_id: str
    answer_label: str
    side: str
    domain: str
    family: str
    amount: float
    estimated_debit: float
    shares: float
    agent_probability: float
    market_probability: float
    expected_profit: float
    quote_source: str
    quoted_at: float


@dataclass(frozen=True)
class Policy:
    bankroll: float = 100
    cash_reserve: float = 40
    max_total: float = 60
    max_market: float = 20
    max_domain: float = 30
    max_family: float = 30
    min_edge: float = .08
    max_price_move: float = .08
    max_age: float = 300
    max_quote_age: float = 30
    tiers: tuple[float, ...] = (5, 10, 20)
    max_markets: int = 8
    max_nodes: int = 250_000

    def __post_init__(self):
        for key in ('bankroll', 'cash_reserve', 'max_total', 'max_market', 'max_domain', 'max_family', 'max_age', 'max_quote_age'):
            number(getattr(self, key), key)
        number(self.min_edge, 'min_edge', 0, 1)
        number(self.max_price_move, 'max_price_move', 0, 1)
        if not 1 <= self.max_markets <= 10 or self.max_nodes < 1:
            raise InvalidData('Taille du panier invalide')
        if not self.tiers or len(self.tiers) > 3:
            raise InvalidData('1 à 3 paliers requis')
        for t in self.tiers:
            number(t, 'tier', 1)


@dataclass(frozen=True)
class Holdings:
    """Optional user-supplied exposures; never presented as live account data."""
    by_market: dict[str, float] = field(default_factory=dict)
    by_domain: dict[str, float] = field(default_factory=dict)
    by_family: dict[str, float] = field(default_factory=dict)
    total: float = 0


@dataclass(frozen=True)
class Plan:
    selected: tuple[Option, ...]
    estimated_debit: float
    expected_profit: float
    cash_remaining: float
    searched_nodes: int
    optimal_for_supplied_options: bool


def to_dict(value):
    return asdict(value)
