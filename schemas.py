"""Contrat de données d'EdgeX : la seule source de vérité partagée par A, B, C et D.

Règles d'équipe :
- personne ne modifie ce fichier sans passer par D (intégration) ;
- ajouter un champ OPTIONNEL : on l'annonce à l'équipe, puis on l'ajoute ;
- renommer ou supprimer un champ : accord de toute l'équipe.

Convention : un prix est une probabilité implicite entre 0 et 1
(une part achetée au prix a rapporte 1 $ si l'événement se produit, 0 sinon).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
Side = Literal["YES", "NO"]
Confidence = Literal["low", "medium", "high"]
Decision = Literal["BUY_YES", "BUY_NO", "SKIP"]
StepKind = Literal[
    "market", "search", "read", "estimate", "critic",
    "revise", "risk", "trade", "skip", "error",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short_id() -> str:
    return uuid4().hex[:8]


class Market(BaseModel):
    """Un marché binaire (question OUI / NON). Produit par B."""

    id: str
    question: str
    category: str | None = None
    end_date: datetime | None = None
    volume_usd: float | None = Field(default=None, ge=0)
    active: bool = True


class Quote(BaseModel):
    """Cotation d'un marché à un instant donné. Produit par B."""

    market_id: str
    yes_bid: Probability  # meilleur prix pour VENDRE du OUI
    yes_ask: Probability  # meilleur prix pour ACHETER du OUI
    no_ask: Probability | None = None  # si absent : on prend 1 - yes_bid
    last_price: Probability | None = None
    timestamp: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _bid_not_above_ask(self) -> Quote:
        if self.yes_bid > self.yes_ask:
            raise ValueError("yes_bid doit être inférieur ou égal à yes_ask")
        return self

    @property
    def spread(self) -> float:
        """Largeur de la fourchette : plus elle est grande, moins le marché est liquide."""
        return self.yes_ask - self.yes_bid

    @property
    def implied_probability(self) -> float:
        """Probabilité affichée par le marché : milieu de la fourchette."""
        return (self.yes_bid + self.yes_ask) / 2

    def ask_for(self, side: Side) -> float:
        """Prix réellement payé pour acheter une part du côté demandé."""
        if side == "YES":
            return self.yes_ask
        return self.no_ask if self.no_ask is not None else 1.0 - self.yes_bid


class Evidence(BaseModel):
    """Une source trouvée par l'agent, pour ou contre l'hypothèse."""

    title: str
    url: str | None = None
    summary: str
    stance: Literal["for", "against"]


class Forecast(BaseModel):
    """Estimation de l'agent. Produit par A."""

    probability: Probability  # probabilité que la réponse soit OUI
    confidence: Confidence
    evidence_for: list[Evidence] = Field(default_factory=list)
    evidence_against: list[Evidence] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    rationale: str = ""


class OrderRequest(BaseModel):
    """Ordre FICTIF demandé par l'agent (A) au broker (B)."""

    market_id: str
    side: Side
    amount_usd: float = Field(gt=0)


class RiskDecision(BaseModel):
    """Verdict du moteur de risque. Produit par D."""

    approved: bool
    requested_usd: float = Field(default=0.0, ge=0)
    max_amount_usd: float = Field(ge=0)  # montant maximal autorisé sur ce côté
    edge: float  # probabilité estimée du côté - prix d'achat
    price: Probability  # prix d'achat utilisé pour le calcul
    reasons: list[str] = Field(default_factory=list)


class Trade(BaseModel):
    """Transaction fictive exécutée. Produit par B."""

    trade_id: str = Field(default_factory=_short_id)
    market_id: str
    side: Side
    amount_usd: float = Field(gt=0)
    price: float = Field(gt=0, le=1)
    shares: float = Field(gt=0)
    timestamp: datetime = Field(default_factory=_now)


class Position(BaseModel):
    market_id: str
    side: Side
    shares: float = Field(ge=0)
    cost_usd: float = Field(ge=0)

    @property
    def avg_price(self) -> float:
        return self.cost_usd / self.shares if self.shares else 0.0


class Portfolio(BaseModel):
    """Portefeuille fictif. Produit par B."""

    cash_usd: float = Field(ge=0)
    positions: list[Position] = Field(default_factory=list)
    trades: list[Trade] = Field(default_factory=list)

    @property
    def exposure_usd(self) -> float:
        """Montant engagé dans les positions ouvertes (au prix d'achat)."""
        return sum(p.cost_usd for p in self.positions)

    @property
    def bankroll_usd(self) -> float:
        """Capital de référence du moteur de risque : cash + montant engagé."""
        return self.cash_usd + self.exposure_usd


class AgentStep(BaseModel):
    """Un événement de la trace. Émis par A, affiché par C."""

    kind: StepKind
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_now)


class RunResult(BaseModel):
    """Résultat complet d'un run de l'agent sur un marché. Produit par A, affiché par C."""

    run_id: str = Field(default_factory=_short_id)
    market: Market
    quote: Quote
    initial_forecast: Forecast | None = None
    final_forecast: Forecast | None = None
    decision: Decision
    skip_reason: str | None = None
    requested_order: OrderRequest | None = None
    risk: RiskDecision | None = None
    trade: Trade | None = None
    steps: list[AgentStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> RunResult:
        if self.decision == "SKIP" and self.trade is not None:
            raise ValueError("Décision SKIP mais un trade est présent")
        if self.trade is not None and self.decision != f"BUY_{self.trade.side}":
            raise ValueError("La décision ne correspond pas au côté du trade")
        return self

    def summary(self) -> dict[str, Any]:
        """Résumé au format du document de conception (en-tête du dashboard)."""
        final = self.final_forecast
        return {
            "market_probability": round(self.quote.implied_probability, 3),
            "initial_estimate": self.initial_forecast.probability if self.initial_forecast else None,
            "revised_estimate": final.probability if final else None,
            "confidence": final.confidence if final else None,
            "decision": self.decision,
            "position": self.trade.amount_usd if self.trade else 0.0,
        }


class RiskConfig(BaseModel):
    """Paramètres du moteur de risque (modifiables sans toucher au code)."""

    min_edge: float = 0.08  # edge minimal pour agir
    kelly_fraction: float = 0.25  # on mise 1/4 de Kelly
    max_position_pct: float = 0.05  # 5 % du capital max par position
    max_total_exposure_pct: float = 0.30  # 30 % du capital max engagé au total
    max_spread: float = 0.10  # fourchette maximale acceptée
    min_trade_usd: float = 1.0
    confidence_multiplier: dict[str, float] = Field(
        default_factory=lambda: {"low": 0.0, "medium": 0.5, "high": 1.0}
    )
