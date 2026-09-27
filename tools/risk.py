"""Constraints gate déterministe d'EdgeX : évaluer, borner, expliquer.

A conserve la responsabilité de sa probabilité. D ne l'ajuste pas : il vérifie
le contrat, la fraîcheur, le sens de l'ordre et l'exposition du portefeuille.
Les réserves qualitatives imposent des plafonds discrets U/2U/4U ; elles ne
s'additionnent jamais pour compenser un blocage.

Ce fichier contient les contrats de D, evaluate_risk et allocate_batch (calculs purs).
Un RiskAssessment admissible n'est PAS une permission de mise. Seule
GateService, dans tools/integration.py, peut réserver les fonds et produire une
Authorization persistée après prévisualisation et revérification atomique.
Les paramètres par défaut sont une politique de démonstration, non calibrée.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tools.tracker import available_units, finite, fingerprint, mana, units

EPS = 1e-12


def probability(value: float, name: str) -> float:
    value = finite(value, name, minimum=0)
    if value > 1:
        raise ValueError(f"{name}: probabilité supérieure à 1")
    return value


def nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: chaîne non vide attendue")


def normalize_url(url: str) -> str:
    """Normalisation minimale : ne fusionne pas deux pages d'un même domaine."""
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
            return ""
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    except ValueError:
        return ""


@dataclass(frozen=True)
class MarketSnapshot:
    """Données vérifiées de B ; dates Unix en SECONDES (API Manifold : ms)."""
    market_id: str
    question: str
    description: str
    probability: float
    token: str
    outcome_type: str
    mechanism: str
    is_resolved: bool
    is_closed: bool
    close_time: float | None
    fetched_at: float

    @property
    def conditions_hash(self) -> str:
        return fingerprint({k: getattr(self, k) for k in (
            "market_id", "question", "description", "token", "outcome_type", "mechanism", "close_time")})

    def validate(self) -> None:
        nonempty(self.market_id, "market_id")
        nonempty(self.question, "question")
        nonempty(self.description, "description/critères de résolution")
        probability(self.probability, "market.probability")
        finite(self.fetched_at, "market.fetched_at", minimum=0)
        if self.close_time is not None:
            finite(self.close_time, "close_time", minimum=0)
        for flag in (self.is_resolved, self.is_closed):
            if not isinstance(flag, bool):
                raise ValueError("Statut du marché inconnu")


@dataclass(frozen=True)
class EvidenceRecord:
    title: str
    url: str
    summary: str
    supports: bool


@dataclass(frozen=True)
class AnalysisEnvelope:
    """AgentAnalysis existant, entouré de métadonnées créées par le programme.

    traced_urls et critic_completed viennent des réponses/outils enregistrés
    par BudgetSession. Ils ne doivent jamais provenir d'un champ déclaré par A.
    market_probability est la probabilité YES réellement capturée avant A.
    """
    analysis_id: str
    market_id: str
    reference_at: float
    completed_at: float
    conditions_hash: str
    market_probability: float
    initial_probability: float
    final_probability: float
    confidence: str
    decision: str
    evidence: tuple[EvidenceRecord, ...]
    reasoning: str
    traced_urls: tuple[str, ...]
    critic_completed: bool
    budget_session_id: str

    @classmethod
    def from_dict(cls, value: dict) -> "AnalysisEnvelope":
        data = dict(value)
        data.pop("edge", None)  # dérivé et recalculé par Tracker, jamais une autorité
        data["evidence"] = tuple(EvidenceRecord(**e) if isinstance(e, dict) else e for e in data["evidence"])
        data["traced_urls"] = tuple(data["traced_urls"])
        return cls(**data)

    def validate(self) -> None:
        for name in ("analysis_id", "market_id", "conditions_hash", "budget_session_id"):
            nonempty(getattr(self, name), name)
        for name in ("market_probability", "initial_probability", "final_probability"):
            probability(getattr(self, name), name)
        for name in ("reference_at", "completed_at"):
            finite(getattr(self, name), name, minimum=0)
        if self.completed_at < self.reference_at:
            raise ValueError("Analyse terminée avant son observation de référence")
        if self.confidence not in ("low", "medium", "high"):
            raise ValueError("confidence invalide")
        if self.decision not in ("BUY_YES", "BUY_NO", "SKIP"):
            raise ValueError("decision invalide")
        if not isinstance(self.critic_completed, bool):
            raise ValueError("critic_completed doit être un booléen")
        for e in self.evidence:
            if not isinstance(e, EvidenceRecord) or not isinstance(e.supports, bool):
                raise ValueError("Preuve invalide")
            nonempty(e.title, "evidence.title")
            nonempty(e.summary, "evidence.summary")
            if not normalize_url(e.url):
                raise ValueError("URL de preuve invalide")


@dataclass(frozen=True)
class PortfolioOrder:
    """Vue par pari de B, pas une valorisation de portefeuille.

    debit inclut les frais déjà débités ; remaining_debit borne le reliquat
    frais compris. position_open=False seulement après fermeture/règlement
    confirmé. external_id doit correspondre au véritable betId Manifold.
    """
    external_id: str
    market_id: str
    outcome: str
    debit: float
    remaining_debit: float
    position_open: bool = True


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Solde brut actuel, avant soustraction des réservations locales de D.

    orders doit comprendre les positions/reliquats externes et les paris connus
    de D (même terminés, pour accuser leur débit dans le solde). Une absence
    entraîne une déduction locale conservatrice, jamais une libération.
    debited_today et realized_loss_today portent sur le compte entier, en UTC.
    """
    account_id: str
    cash_balance: float
    fetched_at: float
    orders: tuple[PortfolioOrder, ...]
    debited_today: float
    realized_loss_today: float

    def validate(self) -> None:
        nonempty(self.account_id, "account_id")
        for key in ("cash_balance", "fetched_at", "debited_today", "realized_loss_today"):
            finite(getattr(self, key), key, minimum=0)
        seen = set()
        for o in self.orders:
            nonempty(o.external_id, "external_id")
            nonempty(o.market_id, "order.market_id")
            if o.external_id in seen or o.outcome not in ("YES", "NO"):
                raise ValueError("Position dupliquée ou sens invalide")
            seen.add(o.external_id)
            if not isinstance(o.position_open, bool):
                raise ValueError("position_open doit être confirmé")
            units(o.debit)
            units(o.remaining_debit)


@dataclass(frozen=True)
class RiskPolicy:
    """Politique fixée par l'équipe ; le LLM n'en est jamais l'auteur."""
    version: str = "edgex-d-v1"
    unit_mana: float = 5
    min_margin: float = 0.08
    exceptional_edge: float = 0.30
    max_market_move: float = 0.12
    max_analysis_age: float = 900
    max_market_age: float = 30
    max_portfolio_age: float = 30
    authorization_ttl: float = 15
    order_ttl: float = 30
    max_market_mana: float = 20
    max_family_mana: float = 30
    max_total_mana: float = 60
    max_daily_debit: float = 60
    max_session_debit: float = 200
    cash_reserve: float = 20
    max_daily_realized_loss: float = 20
    loss_cooldown: float = 900
    require_critic: bool = False
    # Attribution faite par l'équipe, jamais par l'analyse du modèle.
    families: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        nonempty(self.version, "policy.version")
        for key in ("unit_mana", "max_analysis_age", "max_market_age", "max_portfolio_age",
                    "authorization_ttl", "order_ttl", "max_market_mana", "max_family_mana",
                    "max_total_mana", "max_daily_debit", "max_session_debit", "max_daily_realized_loss"):
            if finite(getattr(self, key), key, minimum=0) == 0:
                raise ValueError(f"{key} doit être strictement positif")
        finite(self.cash_reserve, "cash_reserve", minimum=0)
        finite(self.loss_cooldown, "loss_cooldown", minimum=0)
        for key in ("min_margin", "exceptional_edge", "max_market_move"):
            if probability(getattr(self, key), key) == 0:
                raise ValueError(f"{key} doit être positif")
        if self.unit_mana < 1:
            raise ValueError("Unité minimale du MVP : 1 Mana")
        if len(dict(self.families)) != len(self.families):
            raise ValueError("Familles dupliquées")
        for market, family in self.families:
            nonempty(market, "family.market_id")
            nonempty(family, "family_id")

    @property
    def policy_hash(self) -> str:
        return fingerprint(self)

    def family_for(self, market_id: str) -> str:
        return dict(self.families).get(market_id, "UNKNOWN")


@dataclass(frozen=True)
class Check:
    code: str
    result: str                 # PASS, CAP ou STOP
    explanation: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskAssessment:
    eligible: bool
    reason_codes: tuple[str, ...]
    checks: tuple[Check, ...]
    outcome: str | None = None
    directional_edge: float | None = None
    limit_probability: float | None = None
    debit_tiers: tuple[float, ...] = ()
    required_next_step: str = "NONE"


@dataclass(frozen=True)
class Authorization:
    authorization_id: str
    analysis_id: str
    account_id: str
    market_id: str
    family_id: str
    outcome: str
    amount: float               # montant API exact, avant frais
    max_total_debit: float      # plafond total, frais compris
    limit_probability: float   # toujours une probabilité YES
    valid_until: float         # dernier instant pour commencer l'envoi
    order_expires_at: float     # expiration sur Manifold, même sans exécution
    conditions_hash: str
    policy_hash: str
    batch_id: str | None = None
    expected_gain: float | None = None  # hypothèse de la prévisualisation du lot


@dataclass(frozen=True)
class GateDecision:
    decision_id: str
    analysis_id: str
    decided_at: float
    status: str
    reason_codes: tuple[str, ...]
    checks: tuple[Check, ...]
    required_next_step: str
    authorization: Authorization | None = None
    execution_state: str | None = None

    @property
    def approved(self) -> bool:
        return self.status == "APPROVE"

    @classmethod
    def from_dict(cls, data: dict) -> "GateDecision":
        data = dict(data)
        data["reason_codes"] = tuple(data["reason_codes"])
        data["checks"] = tuple(Check(**c) if isinstance(c, dict) else c for c in data["checks"])
        if isinstance(data.get("authorization"), dict):
            data["authorization"] = Authorization(**data["authorization"])
        return cls(**data)


@dataclass(frozen=True)
class PortfolioCapacity:
    """Vue consolidée snapshot + registre, construite par l'intégration."""
    cash_after_pending: float
    by_market: dict[str, float]
    by_family: dict[str, float]
    total: float
    daily_debit_and_pending: float
    session_debit_and_pending: float
    realized_loss_today: float
    paused_reason: str = ""
    unresolved_order: bool = False
    recent_loss_markets: tuple[str, ...] = ()
    recent_loss_families: tuple[str, ...] = ()


def conservative_limit(final_probability: float, outcome: str, margin: float) -> float:
    """Arrondi qui ne détériore jamais la marge : YES bas, NO haut."""
    p, m = Decimal(str(final_probability)), Decimal(str(margin))
    raw = p - m if outcome == "YES" else p + m
    mode = ROUND_FLOOR if outcome == "YES" else ROUND_CEILING
    return float(raw.quantize(Decimal("0.01"), rounding=mode))


def evaluate_risk(analysis: AnalysisEnvelope, market: MarketSnapshot,
                  portfolio: PortfolioSnapshot, capacity: PortfolioCapacity,
                  policy: RiskPolicy, *, now: float) -> RiskAssessment:
    """Contrôles purs. Aucun accès réseau, aucune réservation, aucune mise."""
    checks: list[Check] = []

    def stop(code: str, text: str, next_step: str = "REVIEW", **values) -> RiskAssessment:
        checks.append(Check(code, "STOP", text, values))
        return RiskAssessment(False, (code,), tuple(checks), required_next_step=next_step)

    try:
        analysis.validate()
        market.validate()
        portfolio.validate()
        finite(now, "now", minimum=0)
    except (ValueError, TypeError, AttributeError) as exc:
        return stop("INVALID_DATA", str(exc), "FIX_INPUT")

    if analysis.decision == "SKIP":
        return stop("AGENT_SKIP", "A ne propose pas de mise.", "NONE")
    if analysis.market_id != market.market_id:
        return stop("MARKET_ID_MISMATCH", "A et B ne désignent pas le même marché.", "FIX_INPUT")
    if market.token != "MANA" or market.outcome_type != "BINARY" or market.mechanism != "cpmm-1":
        return stop("UNSUPPORTED_MARKET", "MVP limité à MANA / BINARY / cpmm-1.")
    if market.close_time is None:
        return stop("MISSING_CLOSE_TIME", "Échéance non fournie par B.", "REFRESH")
    if market.is_resolved or market.is_closed or now >= market.close_time:
        return stop("MARKET_CLOSED", "Marché fermé ou résolu.", "NONE")
    checks.append(Check("MARKET_VALID", "PASS", "Identité et contrat recevables."))

    if capacity.paused_reason:
        return stop("KILL_SWITCH", capacity.paused_reason, "OPERATOR")
    if capacity.unresolved_order:
        return stop("UNRESOLVED_ORDER", "Un envoi précédent a une issue inconnue.", "RECONCILE")
    for name, at, age in (("ANALYSIS", analysis.reference_at, policy.max_analysis_age),
                          ("MARKET", market.fetched_at, policy.max_market_age),
                          ("PORTFOLIO", portfolio.fetched_at, policy.max_portfolio_age)):
        if at > now or now - at > age:
            return stop(f"STALE_{name}", "Horodatage futur ou délai dépassé.", "REFRESH", age=now-at, maximum=age)
    if analysis.completed_at > now:
        return stop("FUTURE_ANALYSIS", "Fin d'analyse située dans le futur.", "FIX_INPUT")
    if analysis.conditions_hash != market.conditions_hash:
        return stop("CONDITIONS_CHANGED", "Les conditions ne sont plus celles analysées.", "REANALYSE")
    move = abs(market.probability - analysis.market_probability)
    if move > policy.max_market_move + EPS:
        return stop("MARKET_MOVED", "Mouvement excessif dans un sens ou l'autre.", "REANALYSE", movement=move)
    checks.append(Check("FRESHNESS", "PASS", "Dossier et snapshots encore utilisables.", {"market_movement": move}))

    outcome = "YES" if analysis.decision == "BUY_YES" else "NO"
    edge = (1 if outcome == "YES" else -1) * (analysis.final_probability - market.probability)
    if edge <= 0:
        return stop("WRONG_DIRECTION", "L'estimation de A ne justifie pas ce sens au prix actuel.", "REANALYSE", edge=edge)
    if edge + EPS < policy.min_margin:
        return stop("INSUFFICIENT_EDGE", "Marge restante inférieure à la politique.", "WAIT_OR_REANALYSE", edge=edge, minimum=policy.min_margin)
    checks.append(Check("DIRECTIONAL_EDGE", "PASS", "Marge recalculée dans le sens de l'ordre.", {"edge": edge, "outcome": outcome, "market_probability": market.probability, "final_probability": analysis.final_probability}))

    family = policy.family_for(market.market_id)
    if capacity.by_market.get(market.market_id, 0) > 0:
        return stop("EXISTING_EXPOSURE", "Pas de renforcement ni d'inversion automatique dans ce MVP.", "NONE")
    if market.market_id in capacity.recent_loss_markets or family in capacity.recent_loss_families:
        return stop("LOSS_COOLDOWN", "Pause après une perte sur ce marché ou sa famille.", "WAIT")
    if capacity.realized_loss_today >= policy.max_daily_realized_loss:
        return stop("DAILY_LOSS_STOP", "L'enveloppe de pertes réalisées du jour est épuisée.", "WAIT_OR_OPERATOR")
    if analysis.confidence == "low":
        return stop("LOW_CONFIDENCE", "A déclare lui-même une confiance faible.", "REANALYSE")
    if policy.require_critic and not analysis.critic_completed:
        return stop("CRITIC_REQUIRED", "La politique exige une phase Critic terminée.", "REANALYSE")

    evidence_urls = {normalize_url(e.url) for e in analysis.evidence}
    traced = {normalize_url(url) for url in analysis.traced_urls} - {""}
    if not evidence_urls:
        return stop("NO_EVIDENCE", "A n'a fourni aucune preuve structurée.", "REANALYSE")
    matched = evidence_urls & traced
    if not matched:
        return stop("NO_SOURCE_TRACE", "Aucune preuve ne correspond aux URLs retournées par les outils.", "REANALYSE")

    multiplier = 4
    def cap(value: int, code: str, explanation: str, **values) -> None:
        nonlocal multiplier
        multiplier = min(multiplier, value)
        checks.append(Check(code, "CAP", explanation, {"maximum_units": value, **values}))

    if analysis.confidence == "medium":
        cap(1, "MEDIUM_CONFIDENCE", "Engagement exploratoire seulement.")
    if matched != evidence_urls:
        cap(2, "PARTIAL_TRACE", "Une partie seulement des preuves est traçable.", matched=len(matched), cited=len(evidence_urls))
    else:
        checks.append(Check("SOURCE_TRACE", "PASS", "Références retrouvées dans les outils, sans jugement de vérité."))
    if not analysis.critic_completed:
        cap(1, "NO_CRITIC", "Critic non exécuté ; ne pas simuler sa confirmation.")
    else:
        revision = abs(analysis.final_probability - analysis.initial_probability)
        switched = ((analysis.initial_probability - analysis.market_probability) *
                    (analysis.final_probability - analysis.market_probability)) < 0
        if switched or revision + EPS >= edge:
            cap(1, "FORECAST_REVISION", "Révision importante par rapport à la marge, ou changement de côté.", revision=revision, edge=edge, side_changed=switched)
    if edge + EPS >= policy.exceptional_edge:
        cap(1, "EXCEPTIONAL_DISAGREEMENT", "Un grand désaccord ne prouve pas une grande fiabilité.", edge=edge)

    # Les capacités sont calculées en unités entières : un arrondi flottant
    # ne doit pas autoriser un palier juste au-dessus d'une limite.
    room_units = {
        "CASH": available_units(capacity.cash_after_pending) - units(policy.cash_reserve),
        "MARKET": available_units(policy.max_market_mana) - units(capacity.by_market.get(market.market_id, 0)),
        "FAMILY": available_units(policy.max_family_mana) - units(capacity.by_family.get(family, 0)),
        "TOTAL": available_units(policy.max_total_mana) - units(capacity.total),
        "DAILY": available_units(policy.max_daily_debit) - units(capacity.daily_debit_and_pending),
        "SESSION": available_units(policy.max_session_debit) - units(capacity.session_debit_and_pending),
    }
    room = {key: mana(value) for key, value in room_units.items()}
    tiers = []
    for k in (4, 2, 1):
        if k > multiplier:
            continue
        debit = mana(units(policy.unit_mana) * k)
        blockers = [key for key, value in room_units.items() if units(debit) > value]
        if blockers:
            checks.append(Check("CAPACITY_REDUCTION", "CAP", "Ce palier dépasserait un plafond.", {"debit": debit, "constraints": blockers, "room": room}))
        else:
            tiers.append(debit)
    if not tiers:
        return stop("NO_EXECUTABLE_SIZE", "Aucun palier ne tient dans les enveloppes restantes.", "WAIT_OR_OPERATOR", room=room)

    limit = conservative_limit(analysis.final_probability, outcome, policy.min_margin)
    if not 0 < limit < 1:
        return stop("INVALID_LIMIT", "Aucune limite de prix admissible.", "NONE")
    if (outcome == "YES" and market.probability > limit + EPS) or (outcome == "NO" and market.probability < limit - EPS):
        return stop("LIMIT_NOT_MARKETABLE", "L'arrondi prudent place la limite hors du marché actuel.", "WAIT")
    checks.append(Check("SIZE_AND_LIMIT", "PASS", "Paliers admissibles, coûts compris.", {"tiers": tiers, "limit_probability": limit, "family": family}))
    return RiskAssessment(True, (), tuple(checks), outcome, edge, limit, tuple(tiers))


@dataclass(frozen=True)
class BatchPolicy:
    """Allocation d'un lot fini, pas une règle d'arrêt de type secrétaire.

    Au plus 10 marchés, 3 paliers + ne rien miser : recherche exacte bornée
    à 4**10 combinaisons. Le délai de collecte empêche de commencer une nouvelle
    analyse trop tard ; il n'interrompt pas un appel synchrone déjà commencé.
    Les valeurs espérées restent conditionnelles à A et à la prévisualisation.
    """
    max_analyses: int = 10
    max_collection_seconds: float = 300
    max_quote_age: float = 20
    max_quote_probability_move: float = 0.0
    min_expected_gain: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.max_analyses, bool) or not isinstance(self.max_analyses, int) or not 1 <= self.max_analyses <= 10:
            raise ValueError("Lot limité à 1..10 marchés pour le solveur exact")
        for key in ("max_collection_seconds", "max_quote_age"):
            if finite(getattr(self, key), key, minimum=0) <= 0:
                raise ValueError(f"{key} doit être positif")
        probability(self.max_quote_probability_move, "max_quote_probability_move")
        finite(self.min_expected_gain, "min_expected_gain", minimum=0)


@dataclass(frozen=True)
class AllocationOption:
    """UN palier complet d'UN marché ; les paliers ne sont pas cumulables.

    debit_cap est le montant à réserver (frais inclus), expected_gain le gain
    net ESTIMÉ pour ce palier. option_id est une empreinte des données de B.
    """
    option_id: str
    analysis_id: str
    market_id: str
    family_id: str
    debit_cap: float
    expected_gain: float


@dataclass(frozen=True)
class AllocationResult:
    selected: tuple[AllocationOption, ...]
    expected_gain: float
    total_reserved: float
    searched_nodes: int
    optimal: bool = True


def expected_net_gain(final_probability: float, outcome: str, *,
                      payout_if_correct: float, payout_if_incorrect: float,
                      estimated_debit: float) -> float:
    """Espérance d'un achat binaire sous A, frais compris, à exécution estimée.

    Tous les payouts sont BRUTS (capital récupéré compris), pas des profits.
    Ce calcul n'inclut ni probabilité de fill, ni réinvestissement, ni règlement
    CANCEL/MKT. Il ne remplace pas la borne garantie du débit d'autorisation.
    """
    p = Decimal(str(probability(final_probability, "final_probability")))
    if outcome not in ("YES", "NO"):
        raise ValueError("Sens inconnu")
    if outcome == "NO":
        p = 1 - p
    win = Decimal(str(finite(payout_if_correct, "payout_if_correct", minimum=0)))
    lose = Decimal(str(finite(payout_if_incorrect, "payout_if_incorrect", minimum=0)))
    debit = Decimal(str(finite(estimated_debit, "estimated_debit", minimum=0)))
    if win <= lose:
        raise ValueError("Payouts incohérents pour un achat simple")
    return float(p * win + (1-p) * lose - debit)


def allocate_batch(options: tuple[AllocationOption, ...] | list[AllocationOption],
                   capacity: PortfolioCapacity, policy: RiskPolicy,
                   batch_policy: BatchPolicy = BatchPolicy()) -> AllocationResult:
    """Sac à dos multi-choix exact avec plafonds globaux et par famille.

    Les hard stops et plafonds individuels DOIVENT être appliqués avant l'appel.
    On choisit 0 ou 1 palier par marché. Les réservations emploient des entiers
    millionièmes de Mana ; la somme des gains est calculée en Decimal.
    À gain égal : moins de capital réservé, puis IDs triés (pas l'ordre d'arrivée).
    Une somme d'espérances ne nécessite pas d'indépendance ; elle ne contrôle
    toutefois ni les pertes conjointes ni la qualité des probabilités de A.
    """
    groups: dict[str, list[AllocationOption]] = {}
    market_owner: dict[str, str] = {}
    seen = set()
    for option in options:
        for name in ("option_id", "analysis_id", "market_id", "family_id"):
            nonempty(getattr(option, name), name)
        if option.option_id in seen:
            raise ValueError("Option dupliquée")
        seen.add(option.option_id)
        if option.family_id != policy.family_for(option.market_id):
            raise ValueError("Famille différente de la politique")
        if option.market_id in market_owner and market_owner[option.market_id] != option.analysis_id:
            raise ValueError("Deux analyses du même marché dans le lot")
        market_owner[option.market_id] = option.analysis_id
        units(option.debit_cap)
        if option.debit_cap <= 0:
            raise ValueError("Palier nul ou négatif")
        finite(option.expected_gain, "expected_gain")
        group = groups.setdefault(option.analysis_id, [])
        if group and (group[0].market_id, group[0].family_id) != (option.market_id, option.family_id):
            raise ValueError("Une analyse doit désigner un seul marché")
        group.append(option)
    if len(groups) > batch_policy.max_analyses or any(len(g) > 3 for g in groups.values()):
        raise ValueError("Au plus 10 analyses, et 3 paliers par analyse")

    budget = min(
        available_units(capacity.cash_after_pending) - units(policy.cash_reserve),
        available_units(policy.max_total_mana) - units(capacity.total),
        available_units(policy.max_daily_debit) - units(capacity.daily_debit_and_pending),
        available_units(policy.max_session_debit) - units(capacity.session_debit_and_pending),
    )
    if (budget <= 0 or capacity.paused_reason or capacity.unresolved_order
            or capacity.realized_loss_today >= policy.max_daily_realized_loss):
        return AllocationResult((), 0.0, 0.0, 0)

    candidates = []
    for aid in sorted(groups):
        valid = [o for o in groups[aid]
                 if o.expected_gain > batch_policy.min_expected_gain
                 and capacity.by_market.get(o.market_id, 0) <= 0
                 and o.market_id not in capacity.recent_loss_markets
                 and o.family_id not in capacity.recent_loss_families
                 and units(o.debit_cap) <= available_units(policy.max_market_mana)]
        # Éliminer seulement les options réellement dominées du MÊME marché.
        valid = [o for o in valid if not any(
            units(other.debit_cap) <= units(o.debit_cap) and other.expected_gain >= o.expected_gain
            and (units(other.debit_cap), -other.expected_gain, other.option_id)
                < (units(o.debit_cap), -o.expected_gain, o.option_id)
            for other in valid if other is not o)]
        if valid:
            candidates.append(sorted(valid, key=lambda o: (-o.expected_gain, o.debit_cap, o.option_id)))

    family_room = {o.family_id: max(0, available_units(policy.max_family_mana)
                   - units(capacity.by_family.get(o.family_id, 0)))
                   for group in candidates for o in group}
    suffix = [Decimal(0)] * (len(candidates) + 1)
    for i in range(len(candidates)-1, -1, -1):
        suffix[i] = suffix[i+1] + max(Decimal(str(o.expected_gain)) for o in candidates[i])
    best_score, best_cost, best_ids = Decimal(0), 0, ()
    best: tuple[AllocationOption, ...] = ()
    nodes = 0
    used_family: dict[str, int] = {}

    def visit(i: int, cost: int, score: Decimal, chosen: tuple[AllocationOption, ...]) -> None:
        nonlocal best, best_score, best_cost, best_ids, nodes
        nodes += 1
        if score + suffix[i] < best_score:
            return
        if i == len(candidates):
            ids = tuple(sorted(o.option_id for o in chosen))
            if score > best_score or (score == best_score and (cost, ids) < (best_cost, best_ids)):
                best, best_score, best_cost, best_ids = chosen, score, cost, ids
            return
        for o in candidates[i]:
            debit = units(o.debit_cap)
            old = used_family.get(o.family_id, 0)
            if cost + debit > budget or old + debit > family_room[o.family_id]:
                continue
            used_family[o.family_id] = old + debit
            visit(i+1, cost+debit, score+Decimal(str(o.expected_gain)), chosen+(o,))
            used_family[o.family_id] = old
        visit(i+1, cost, score, chosen)  # Ne pas miser reste toujours possible.

    visit(0, 0, Decimal(0), ())
    return AllocationResult(tuple(sorted(best, key=lambda o: o.market_id)), float(best_score), mana(best_cost), nodes)
