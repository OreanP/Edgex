"""Raccordement de D : B → budget/cache → A → lot de prévisions → gate/allocation → B.

GateService est l'unique chemin de mise à exposer dans l'application. Les clés
Manifold et la méthode place_order de B ne doivent pas être accessibles aux
outils du LLM. Ce module n'implémente pas l'API de B : son contrat Broker précise
les snapshots, la borne de frais, la prévisualisation et la réconciliation.

Une autorisation est persistée et consommable une fois. Avant l'appel réseau,
son état passe atomiquement à SENDING. Toute erreur après cette transition
laisse UNKNOWN/SENDING : aucun retry d'envoi, aucune libération supposée.
La protection porte sur les appels passant par ce service, pas sur un Python
malveillant ayant accès aux clés ou au fichier SQLite.

Utilisation : créer Tracker, Broker de B, GateService, BudgetEngine et Cache ;
appeler recover_pending au démarrage, puis Pipeline.run_cycle. execute=False
est la valeur par défaut. Voir tests/test_d.py pour un parcours hors ligne.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from tools.budget import BudgetEngine, BudgetExceeded
from tools.cache import Cache
from tools.risk import (
    EPS, AnalysisEnvelope, Authorization, Check, EvidenceRecord, GateDecision,
    AllocationOption, BatchPolicy, allocate_batch, expected_net_gain,
    MarketSnapshot, PortfolioCapacity, PortfolioSnapshot, RiskAssessment,
    RiskPolicy, evaluate_risk, normalize_url,
)
from tools.tracker import (
    UNRESOLVED, StateConflict, Tracker, available_units, finite,
    mana, plain, units, utc_day, fingerprint,
)


@dataclass(frozen=True)
class Quote:
    """Borne d'exécution de B, pas seulement un coût observé au dryRun.

    total_debit_upper_bound couvre toute l'exécution future et tous ses frais.
    debit_bound_guaranteed=False entraîne un refus. Ne pas mettre True sur la
    seule foi du coût instantané de la prévisualisation.
    """
    market_id: str
    outcome: str
    amount: float
    total_debit_upper_bound: float
    limit_probability: float
    order_expires_at: float
    quoted_at: float
    executable: bool
    debit_bound_guaranteed: bool
    # Champs supplémentaires exigés UNIQUEMENT par l'allocation en lot.
    # B estime chaque palier séparément (impact de prix et frais compris).
    estimated_debit: float | None = None
    payout_if_correct: float | None = None
    payout_if_incorrect: float | None = None
    full_fill_estimated: bool = False
    reference_probability: float | None = None


@dataclass(frozen=True)
class BatchDecision:
    """Plan local atomiquement réservé, pas une exécution atomique sur Manifold."""
    batch_id: str
    decided_at: float
    decisions: tuple[GateDecision, ...]
    expected_gain: float
    total_reserved: float
    searched_nodes: int
    optimal_for_supplied_options: bool = True

    @classmethod
    def from_dict(cls, value: dict) -> "BatchDecision":
        data = {k: value[k] for k in cls.__dataclass_fields__}
        data["decisions"] = tuple(GateDecision.from_dict(d) for d in data["decisions"])
        return cls(**data)


@dataclass(frozen=True)
class ExecutionReport:
    """Observation cumulative et non ambiguë, fournie par B."""
    state: str
    cumulative_debit: float
    remaining_debit: float
    external_id: str | None
    observed_at: float
    evidence_ref: str


class Broker(Protocol):
    """Interface à implémenter par B, sans retries automatiques de mise.

    Dates en secondes dans D ; B convertit en millisecondes pour Manifold.
    Un HTTP 200 peut signifier OPEN/PARTIAL, pas nécessairement FILLED.
    lookup_order retourne None en cas de doute, jamais un faux REJECTED.
    Le compte doit être dédié, ou tous ses mouvements externes doivent être
    représentés dans les snapshots. B doit borner ses propres appels réseau.
    """
    account_id: str

    def fetch_market(self, market_id: str) -> MarketSnapshot: ...
    def fetch_portfolio(self) -> PortfolioSnapshot: ...
    def preview_order(self, *, market_id: str, outcome: str, max_total_debit: float,
                      limit_probability: float, order_expires_at: float) -> Quote: ...
    def place_order(self, authorization: Authorization) -> ExecutionReport: ...
    def lookup_order(self, authorization: Authorization) -> ExecutionReport | None: ...


def consolidate_portfolio(portfolio: PortfolioSnapshot, rows: list[dict],
                          policy: RiskPolicy, *, now: float, paused: str = "",
                          exclude_authorization: str | None = None) -> PortfolioCapacity:
    """Fusionner les débits par betId, sans compter deux fois le même ordre.

    Si un débit local confirmé n'est pas encore reflété dans le snapshot, il
    reste soustrait du cash. En cas de snapshot moins avancé, on retient la vue
    la plus conservatrice. Une position locale n'est libérée qu'après settle().
    """
    portfolio.validate()
    observed = {o.external_id: o for o in portfolio.orders}
    exposure: dict[str, int] = {}
    pending = 0
    missing_cash = 0
    unreflected_today = 0
    session_spent = 0
    own_today = 0
    own_loss = 0
    unreflected_loss = 0
    recent_markets, recent_families = set(), set()
    day = utc_day(now)
    for o in portfolio.orders:
        total = (units(o.debit) if o.position_open else 0) + units(o.remaining_debit)
        exposure[o.market_id] = exposure.get(o.market_id, 0) + total
        pending += units(o.remaining_debit)

    selected = [r for r in rows if r["id"] != exclude_authorization]
    for r in selected:
        o = observed.get(r["external_id"])
        if o and (o.market_id != r["market_id"] or o.outcome != r["authorization"]["outcome"]):
            raise StateConflict("Le betId du snapshot correspond à un autre contrat")
        spent, remaining = r["spent"], r["remaining"]
        local_exposure = (spent if r["settled_at"] is None else 0) + remaining
        if o:
            external_exposure = (units(o.debit) if o.position_open else 0) + units(o.remaining_debit)
            additional_exposure = max(0, local_exposure - external_exposure)
            missing_debit = max(0, spent - units(o.debit))
            pending += max(0, remaining - units(o.remaining_debit))
        else:
            additional_exposure = local_exposure
            missing_debit = spent
            pending += remaining
        missing_cash += missing_debit
        exposure[r["market_id"]] = exposure.get(r["market_id"], 0) + additional_exposure
        session_spent += max(spent, units(o.debit) if o else 0)
        # Un cumul observé aujourd'hui peut inclure d'anciens fills : surcompter
        # est prudent ; nous ne prétendons pas disposer d'un journal de fills de B.
        if utc_day(r["created_at"]) == day or (r["observed_at"] and utc_day(r["observed_at"]) == day):
            own_today += spent
            unreflected_today += missing_debit
        if r["settled_at"] is not None and r["payout"] < spent:
            loss = spent - r["payout"]
            if utc_day(r["settled_at"]) == day:
                own_loss += loss
                if o is None or o.position_open:
                    unreflected_loss += loss
            if now - r["settled_at"] < policy.loss_cooldown:
                recent_markets.add(r["market_id"])
                recent_families.add(policy.family_for(r["market_id"]))
    families: dict[str, int] = {}
    for market_id, total in exposure.items():
        family = policy.family_for(market_id)
        families[family] = families.get(family, 0) + total
    return PortfolioCapacity(
        cash_after_pending=mana(available_units(portfolio.cash_balance) - pending - missing_cash),
        by_market={k: mana(v) for k, v in exposure.items()},
        by_family={k: mana(v) for k, v in families.items()},
        total=mana(sum(exposure.values())),
        daily_debit_and_pending=mana(max(units(portfolio.debited_today) + unreflected_today, own_today) + pending),
        session_debit_and_pending=mana(session_spent + pending),
        realized_loss_today=mana(max(units(portfolio.realized_loss_today) + unreflected_loss, own_loss)),
        paused_reason=paused,
        unresolved_order=any(r["state"] in UNRESOLVED for r in selected),
        recent_loss_markets=tuple(sorted(recent_markets)),
        recent_loss_families=tuple(sorted(recent_families)),
    )


class GateService:
    """Autorise atomiquement puis exécute exactement l'autorisation enregistrée."""

    def __init__(self, tracker: Tracker, broker: Broker, policy: RiskPolicy = RiskPolicy(),
                 batch_policy: BatchPolicy = BatchPolicy()):
        self.tracker, self.broker, self.policy = tracker, broker, policy
        self.batch_policy = batch_policy

    def _call_b(self, operation: str, function, *args, links: dict | None = None, **kwargs):
        """Tracer le raccordement réel à B ; ne pas inventer sa date fournisseur."""
        with self.tracker.operation("B", operation, payload={"request": kwargs}, **(links or {})) as log:
            result = function(*args, **kwargs)
            log["response"] = plain(result)
            return result

    def fetch_market(self, market_id: str) -> MarketSnapshot:
        return self._call_b("FETCH_MARKET", self.broker.fetch_market, market_id,
                            links={"market_id": market_id})

    def fetch_portfolio(self) -> PortfolioSnapshot:
        return self._call_b("FETCH_PORTFOLIO", self.broker.fetch_portfolio)


    def _reject(self, a: AnalysisEnvelope, code: str, explanation: str,
                *, checks: tuple[Check, ...] = (), next_step: str = "REVIEW", db=None) -> GateDecision:
        decision = GateDecision(uuid4().hex, a.analysis_id, self.tracker.clock(), "REJECT", (code,),
                                checks + (Check(code, "STOP", explanation),), next_step)
        if db is not None:
            self.tracker.save_decision(decision, db)
        else:
            with self.tracker.transaction() as own:
                self.tracker.save_decision(decision, own)
        return decision

    def _proof_error(self, a: AnalysisEnvelope, db) -> str | None:
        """La confiance dans les métadonnées s'appuie sur le registre, pas A."""
        session = db.execute("SELECT * FROM d_budget_sessions WHERE id=?", (a.budget_session_id,)).fetchone()
        if not session or session["state"] != "DONE" or a.analysis_id != a.budget_session_id:
            return "UNMETERED_ANALYSIS"
        if session["market_id"] != a.market_id or not a.reference_at <= session["started_at"] <= a.completed_at:
            return "INVALID_ANALYSIS_CONTEXT"
        rows = db.execute("SELECT phase,state,metadata FROM d_budget_calls WHERE session_id=?", (a.budget_session_id,)).fetchall()
        complete = [r for r in rows if r["state"] == "DONE"]
        if not complete or not any(r["phase"] == "researcher" for r in complete):
            return "NO_RESEARCHER_TRACE"
        expected = {u for r in complete for u in json.loads(r["metadata"]).get("urls", [])}
        claimed = {normalize_url(u) for u in a.traced_urls}
        if expected != claimed or a.critic_completed != any(r["phase"] == "critic" for r in complete):
            return "TRACE_MISMATCH"
        return None

    def _assess(self, a, market, portfolio, db, *, exclude=None) -> RiskAssessment:
        if portfolio.account_id != self.broker.account_id:
            raise StateConflict("Snapshot d'un autre compte")
        self.tracker.bind("account_id", portfolio.account_id, db)
        self.tracker.bind("risk_policy_hash", self.policy.policy_hash, db)
        cap = consolidate_portfolio(portfolio, self.tracker.orders(db), self.policy,
                                    now=self.tracker.clock(), paused=self.tracker.meta("paused", db) or "",
                                    exclude_authorization=exclude)
        return evaluate_risk(a, market, portfolio, cap, self.policy, now=self.tracker.clock())

    def _snapshots(self, market_id: str):
        market = self.fetch_market(market_id)
        portfolio = self.fetch_portfolio()
        market.validate()
        portfolio.validate()
        return market, portfolio

    def review(self, a: AnalysisEnvelope) -> GateDecision:
        """Tracer, contrôler, prévisualiser et réserver ; ne place aucun pari.

        Les erreurs structurelles d'enveloppe lèvent ValueError/StateConflict.
        Les refus de politique ou indisponibilités renvoient GateDecision(REJECT).
        Une collision d'identifiant n'est jamais transformée en nouvelle analyse.
        """
        a.validate()
        self.tracker.record_analysis(a)
        with self.tracker.transaction() as db:
            self.tracker.expire_unsent(self.tracker.clock(), db)
            previous = self.tracker.order_for_analysis(a.analysis_id, db)
            if previous:
                return replace(GateDecision.from_dict(previous["decision"]), execution_state=previous["state"])
            problem = self._proof_error(a, db)
            if problem:
                return self._reject(a, problem, "Métadonnées de A non corroborées par les appels enregistrés.", next_step="FIX_A_INTEGRATION", db=db)
        try:
            market, portfolio = self._snapshots(a.market_id)
        except Exception as exc:
            return self._reject(a, "SNAPSHOT_UNAVAILABLE", type(exc).__name__, next_step="REFRESH")
        try:
            with self.tracker.transaction() as db:
                assessment = self._assess(a, market, portfolio, db)
        except (ValueError, TypeError, StateConflict) as exc:
            return self._reject(a, "INVALID_SNAPSHOT_OR_POLICY", str(exc), next_step="OPERATOR")
        if not assessment.eligible:
            return self._reject(a, assessment.reason_codes[0], "Contrôle de risque non satisfait.", checks=assessment.checks, next_step=assessment.required_next_step)

        now = self.tracker.clock()
        expires = min(now + self.policy.order_ttl, a.reference_at + self.policy.max_analysis_age, market.close_time)
        quote = None
        cap = None
        preview_checks = []
        for debit_cap in assessment.debit_tiers:
            try:
                candidate = self._call_b("PREVIEW_ORDER", self.broker.preview_order,
                    links={"analysis_id": a.analysis_id, "market_id": a.market_id},
                    market_id=a.market_id, outcome=assessment.outcome,
                    max_total_debit=debit_cap, limit_probability=assessment.limit_probability,
                    order_expires_at=expires)
                self._validate_quote(candidate, a, assessment, debit_cap, expires)
            except Exception as exc:
                return self._reject(a, "PREVIEW_UNAVAILABLE_OR_UNSAFE", type(exc).__name__, checks=assessment.checks, next_step="FIX_B_OR_RETRY_PREVIEW")
            if not candidate.executable:
                preview_checks.append(Check("NOT_EXECUTABLE_AT_SIZE", "CAP", "Prévisualisation sans exécution possible.", {"debit_cap": debit_cap}))
                continue
            quote, cap = candidate, debit_cap
            break
        if quote is None:
            return self._reject(a, "NO_EXECUTABLE_SIZE", "Aucun palier prévisualisé n'est exécutable.", checks=assessment.checks + tuple(preview_checks), next_step="WAIT")

        # Le réseau se fait hors transaction ; les réservations concurrentes
        # sont relues ensuite sous verrou, avant toute autorisation.
        try:
            market, portfolio = self._snapshots(a.market_id)
        except Exception as exc:
            return self._reject(a, "REFRESH_FAILED", type(exc).__name__, next_step="REFRESH")
        try:
            with self.tracker.transaction() as db:
                now = self.tracker.clock()
                self.tracker.expire_unsent(now, db)
                previous = self.tracker.order_for_analysis(a.analysis_id, db)
                if previous:
                    return replace(GateDecision.from_dict(previous["decision"]), execution_state=previous["state"])
                current = self._assess(a, market, portfolio, db)
                if not current.eligible or cap not in current.debit_tiers:
                    return self._reject(a, "STATE_CHANGED", "La capacité ou le risque a changé pendant la prévisualisation.", checks=current.checks, next_step="REEVALUATE", db=db)
                self._validate_quote(quote, a, current, cap, expires)
                valid_until = min(now + self.policy.authorization_ttl, expires,
                                  a.reference_at + self.policy.max_analysis_age)
                if valid_until <= now:
                    return self._reject(a, "AUTHORIZATION_EXPIRED", "Le dossier n'a plus de durée de validité.", next_step="REANALYSE", db=db)
                auth = Authorization(uuid4().hex, a.analysis_id, portfolio.account_id, a.market_id,
                                     self.policy.family_for(a.market_id), current.outcome, quote.amount, cap,
                                     current.limit_probability, valid_until, expires,
                                     market.conditions_hash, self.policy.policy_hash)
                checks = current.checks + tuple(preview_checks) + (Check(
                    "AUTHORIZED_SIZE", "PASS", "Ordre exact et plafond de débit total réservés.",
                    {"amount_before_fees": quote.amount, "max_total_debit": cap}),)
                decision = GateDecision(uuid4().hex, a.analysis_id, now, "APPROVE", ("AUTHORIZED",), checks, "EXECUTE_ONCE", auth, "RESERVED")
                self.tracker.reserve(decision, db)
                return decision
        except (ValueError, TypeError, StateConflict) as exc:
            return self._reject(a, "FINAL_CHECK_FAILED", str(exc), next_step="OPERATOR")

    def _replay_batch(self, payload: dict) -> BatchDecision:
        """Relire le plan, jamais renouveler ses autorisations expirées."""
        batch = BatchDecision.from_dict(payload)
        decisions = []
        with self.tracker.transaction() as db:
            self.tracker.expire_unsent(self.tracker.clock(), db)
            for decision in batch.decisions:
                if decision.authorization:
                    row = self.tracker.order(decision.authorization.authorization_id, db)
                    decision = replace(decision, execution_state=row["state"])
                decisions.append(decision)
        return replace(batch, decisions=tuple(decisions))

    def _validate_batch_quote(self, q: Quote, a: AnalysisEnvelope, assessment: RiskAssessment,
                              market: MarketSnapshot, cap: float, expires: float) -> float:
        """Ajouter une valorisation nette exploitable, sans inventer de payout."""
        self._validate_quote(q, a, assessment, cap, expires)
        if q.full_fill_estimated is not True:
            raise ValueError("La valorisation du MVP exige un fill complet estimé")
        if q.estimated_debit is None or q.payout_if_correct is None or q.payout_if_incorrect is None:
            raise ValueError("Coût / payouts absents : edge*montant n'est pas un substitut")
        if not units(q.amount) <= units(q.estimated_debit) <= units(q.total_debit_upper_bound):
            raise ValueError("Coût estimé incompatible avec montant et borne de débit")
        now = self.tracker.clock()
        if not 0 <= now-q.quoted_at <= self.batch_policy.max_quote_age:
            raise ValueError("Valorisation trop ancienne")
        reference = finite(q.reference_probability, "quote.reference_probability", minimum=0)
        if reference > 1 or abs(reference-market.probability) > self.batch_policy.max_quote_probability_move + EPS:
            raise ValueError("Prix de référence de la valorisation modifié")
        return expected_net_gain(a.final_probability, assessment.outcome,
                                 payout_if_correct=q.payout_if_correct,
                                 payout_if_incorrect=q.payout_if_incorrect,
                                 estimated_debit=q.estimated_debit)

    def review_batch(self, analyses: list[AnalysisEnvelope] | tuple[AnalysisEnvelope, ...], *,
                     batch_id: str, collection_errors: list[dict] | None = None) -> BatchDecision:
        """Filtrer, valoriser TOUS les paliers et réserver le meilleur lot.

        L'optimalité ne porte que sur les options valides et prévisualisées,
        sous les probabilités de A. Réseau hors transaction ; résolution et
        réservation commune sous verrou. Aucun appel place_order ici.
        Même batch_id + mêmes entrées => même plan ; un autre contenu est une
        collision. Un lot refusé se réévalue avec un NOUVEL ID de lot.
        """
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError("batch_id obligatoire")
        if len(analyses) > self.batch_policy.max_analyses:
            raise ValueError("Lot trop grand ; aucune troncature silencieuse")
        if len({a.analysis_id for a in analyses}) != len(analyses):
            raise ValueError("Analyse dupliquée dans le lot")
        if len({a.market_id for a in analyses}) != len(analyses):
            raise ValueError("Un seul dossier par marché dans le lot")
        ordered = sorted(analyses, key=lambda a: (a.market_id, a.analysis_id))
        for a in ordered:
            a.validate()
            self.tracker.record_analysis(a)
        digest = fingerprint({"analyses": ordered, "risk": self.policy, "batch": self.batch_policy})
        old = self.tracker.batch(batch_id)
        if old:
            if old["input_digest"] != digest:
                raise StateConflict("BATCH_ID_COLLISION")
            return self._replay_batch(old["payload"])
        with self.tracker.operation("D", "REVIEW_BATCH", batch_id=batch_id,
                                   payload={"analyses": [a.analysis_id for a in ordered]}) as log:
            result = self._review_batch(ordered, batch_id, digest, collection_errors or [])
            log.update({"expected_gain": result.expected_gain, "reserved": result.total_reserved,
                        "searched_nodes": result.searched_nodes})
            return result

    def _review_batch(self, analyses, batch_id, digest, collection_errors) -> BatchDecision:
        problems, markets, assessments, quotes = {}, {}, {}, {}
        # Première photographie : écarter les doublons et les dossiers non tracés.
        with self.tracker.transaction() as db:
            self.tracker.expire_unsent(self.tracker.clock(), db)
            pending = [a for a in analyses if not self.tracker.order_for_analysis(a.analysis_id, db)]
            for a in pending:
                error = self._proof_error(a, db)
                if error:
                    problems[a.analysis_id] = (error, "Métadonnées non corroborées par le registre de A")
        for a in pending:
            if a.analysis_id not in problems:
                try:
                    markets[a.analysis_id] = self.fetch_market(a.market_id)
                except Exception as exc:
                    problems[a.analysis_id] = ("SNAPSHOT_UNAVAILABLE", type(exc).__name__)
        portfolio = None
        try:
            portfolio = self.fetch_portfolio() if pending else None
        except Exception as exc:
            for a in pending:
                problems[a.analysis_id] = ("SNAPSHOT_UNAVAILABLE", type(exc).__name__)

        if portfolio is not None:
            with self.tracker.transaction() as db:
                for a in pending:
                    if a.analysis_id in problems:
                        continue
                    try:
                        assessments[a.analysis_id] = self._assess(a, markets[a.analysis_id], portfolio, db)
                    except (ValueError, TypeError, StateConflict) as exc:
                        problems[a.analysis_id] = ("INVALID_SNAPSHOT_OR_POLICY", type(exc).__name__)
            for a in pending:
                assessment = assessments.get(a.analysis_id)
                if not assessment or not assessment.eligible or a.analysis_id in problems:
                    continue
                expires = min(self.tracker.clock()+self.policy.order_ttl,
                              a.reference_at+self.policy.max_analysis_age,
                              markets[a.analysis_id].close_time)
                choices = []
                for cap in assessment.debit_tiers:
                    try:
                        q = self._call_b("PREVIEW_ORDER", self.broker.preview_order,
                            links={"analysis_id": a.analysis_id, "market_id": a.market_id},
                            market_id=a.market_id, outcome=assessment.outcome,
                            max_total_debit=cap, limit_probability=assessment.limit_probability,
                            order_expires_at=expires)
                        self._validate_batch_quote(q, a, assessment, markets[a.analysis_id], cap, expires)
                        if q.executable:
                            choices.append((cap, expires, q))
                    except Exception as exc:
                        self.tracker.log_event("D", "QUOTE_DISCARDED", status="SKIPPED",
                            analysis_id=a.analysis_id, market_id=a.market_id,
                            payload={"cap": cap, "error_type": type(exc).__name__})
                quotes[a.analysis_id] = choices
                if not choices:
                    problems[a.analysis_id] = ("NO_VALUED_OPTION", "Aucun palier avec exécution et valorisation sûres")

        # Reprendre les prix après les prévisualisations. Le portefeuille commun
        # est lu en dernier, puis tous les engagements locaux sont relus sous verrou.
        for a in pending:
            if quotes.get(a.analysis_id):
                try:
                    markets[a.analysis_id] = self.fetch_market(a.market_id)
                except Exception as exc:
                    problems[a.analysis_id] = ("REFRESH_FAILED", type(exc).__name__)
        if pending:
            try:
                portfolio = self.fetch_portfolio()
            except Exception as exc:
                for a in pending:
                    problems[a.analysis_id] = ("REFRESH_FAILED", type(exc).__name__)

        replay = None
        with self.tracker.transaction() as db:
            self.tracker.expire_unsent(self.tracker.clock(), db)
            old = self.tracker.batch(batch_id, db)
            if old:
                if old["input_digest"] != digest:
                    raise StateConflict("BATCH_ID_COLLISION")
                replay = old["payload"]
            else:
                self.tracker.bind("batch_policy_hash", fingerprint(self.batch_policy), db)
                decisions, final_assessments, options, option_quotes = {}, {}, [], {}
                for a in analyses:
                    previous = self.tracker.order_for_analysis(a.analysis_id, db)
                    if previous:
                        decisions[a.analysis_id] = replace(GateDecision.from_dict(previous["decision"]),
                                                           execution_state=previous["state"])
                        continue
                    with self.tracker.log_context(analysis_id=a.analysis_id, market_id=a.market_id):
                        problem = problems.get(a.analysis_id)
                        trace_error = self._proof_error(a, db)
                        if trace_error:
                            problem = (trace_error, "Trace A invalide")
                        if problem:
                            decisions[a.analysis_id] = self._reject(a, *problem, next_step="REFRESH_OR_FIX_INPUT", db=db)
                            continue
                        try:
                            current = self._assess(a, markets[a.analysis_id], portfolio, db)
                        except (ValueError, TypeError, StateConflict) as exc:
                            decisions[a.analysis_id] = self._reject(a, "FINAL_CHECK_FAILED", type(exc).__name__, db=db)
                            continue
                        if not current.eligible:
                            decisions[a.analysis_id] = self._reject(a, current.reason_codes[0], "Hard stop individuel",
                                checks=current.checks, next_step=current.required_next_step, db=db)
                            continue
                        final_assessments[a.analysis_id] = current
                        for cap, expires, q in quotes.get(a.analysis_id, []):
                            if cap not in current.debit_tiers:
                                continue
                            try:
                                gain = self._validate_batch_quote(q, a, current, markets[a.analysis_id], cap, expires)
                            except (ValueError, TypeError):
                                continue
                            if gain <= self.batch_policy.min_expected_gain:
                                continue
                            option = AllocationOption(fingerprint({"analysis_id": a.analysis_id, "quote": q, "cap": cap}),
                                a.analysis_id, a.market_id, self.policy.family_for(a.market_id), cap, gain)
                            options.append(option)
                            option_quotes[option.option_id] = q
                        if not any(o.analysis_id == a.analysis_id for o in options):
                            decisions[a.analysis_id] = self._reject(a, "NO_POSITIVE_FRESH_OPTION",
                                "Aucun palier valorisé, frais compris, encore valide et positif", checks=current.checks,
                                next_step="REQUOTE_OR_WAIT", db=db)

                # Toutes les options utilisent la même capacité, y compris les
                # réservations d'autres cycles créées pendant le travail réseau.
                if options:
                    capacity = consolidate_portfolio(portfolio, self.tracker.orders(db), self.policy,
                        now=self.tracker.clock(), paused=self.tracker.meta("paused", db) or "")
                    plan = allocate_batch(options, capacity, self.policy, self.batch_policy)
                else:
                    from tools.risk import AllocationResult
                    plan = AllocationResult((), 0, 0, 0)
                expired_during_solve = False
                by_id = {a.analysis_id: a for a in analyses}
                for o in plan.selected:
                    a = by_id[o.analysis_id]
                    try:
                        updated = self._assess(a, markets[a.analysis_id], portfolio, db)
                        if not updated.eligible or o.debit_cap not in updated.debit_tiers:
                            raise ValueError("Dossier périmé pendant la résolution")
                        q = option_quotes[o.option_id]
                        self._validate_batch_quote(q, a, updated, markets[a.analysis_id], o.debit_cap, q.order_expires_at)
                    except (ValueError, TypeError, StateConflict):
                        expired_during_solve = True
                if expired_during_solve:
                    from tools.risk import AllocationResult
                    plan = AllocationResult((), 0, 0, plan.searched_nodes, optimal=False)
                selected = {o.analysis_id: o for o in plan.selected}
                now = self.tracker.clock()
                for a in analyses:
                    if a.analysis_id in decisions:
                        continue
                    current = final_assessments[a.analysis_id]
                    option = selected.get(a.analysis_id)
                    if option is None:
                        decisions[a.analysis_id] = self._reject(a,
                            "BATCH_EXPIRED_DURING_SOLVE" if expired_during_solve else "BATCH_NOT_SELECTED",
                            "Dossier périmé pendant le calcul" if expired_during_solve else
                            "Admissible, mais non retenu dans l'allocation optimale des enveloppes disponibles",
                            checks=current.checks, next_step="NONE", db=db)
                        continue
                    q = option_quotes[option.option_id]
                    auth = Authorization(uuid4().hex, a.analysis_id, portfolio.account_id, a.market_id,
                        option.family_id, current.outcome, q.amount, option.debit_cap, current.limit_probability,
                        min(now+self.policy.authorization_ttl, q.order_expires_at), q.order_expires_at,
                        markets[a.analysis_id].conditions_hash, self.policy.policy_hash, batch_id, option.expected_gain)
                    checks = current.checks + (Check("BATCH_ALLOCATION", "PASS",
                        "Palier choisi collectivement, gain estimé net des frais ; aucune garantie de résultat",
                        {"batch_id": batch_id, "expected_gain": option.expected_gain,
                         "reserved": option.debit_cap, "estimated_debit": q.estimated_debit,
                         "quote_at": q.quoted_at}),)
                    decision = GateDecision(uuid4().hex, a.analysis_id, now, "APPROVE", ("BATCH_SELECTED",),
                                            checks, "EXECUTE_ONCE", auth, "RESERVED")
                    self.tracker.reserve(decision, db)
                    decisions[a.analysis_id] = decision
                result = BatchDecision(batch_id, now, tuple(decisions[a.analysis_id] for a in analyses),
                                       plan.expected_gain, plan.total_reserved, plan.searched_nodes, plan.optimal)
                payload = {**plain(result), "collection_errors": collection_errors,
                           "options": [{"option": plain(o), "quote": plain(option_quotes[o.option_id])} for o in options],
                           "batch_policy": plain(self.batch_policy), "risk_policy_hash": self.policy.policy_hash,
                           "objective": "net_expected_gain_given_A_and_full_fill_quote"}
                self.tracker.save_batch(batch_id, digest, payload, db)
        return self._replay_batch(replay) if replay is not None else result

    def _validate_quote(self, q: Quote, a: AnalysisEnvelope, assessment: RiskAssessment,
                        cap: float, expires: float) -> None:
        now = self.tracker.clock()
        if not isinstance(q, Quote) or q.debit_bound_guaranteed is not True:
            raise ValueError("Borne de frais non garantie par B")
        if not isinstance(q.executable, bool):
            raise ValueError("executable doit être confirmé")
        if q.market_id != a.market_id or q.outcome != assessment.outcome:
            raise ValueError("Contrat ou sens modifié par B")
        if q.limit_probability != assessment.limit_probability or q.order_expires_at != expires:
            raise ValueError("Prix limite / expiration modifiés par B")
        if units(q.total_debit_upper_bound) > units(cap) or units(q.amount) > units(q.total_debit_upper_bound):
            raise ValueError("Débit maximal dépassé")
        if finite(q.amount, "amount", minimum=0) < 1:
            raise ValueError("Le MVP exige un montant API >= 1 Mana")
        finite(q.quoted_at, "quoted_at", minimum=0)
        if not 0 <= now - q.quoted_at <= self.policy.max_market_age or expires <= now:
            raise ValueError("Prévisualisation périmée")

    def execute(self, authorization_id: str) -> dict:
        """Consommer une autorisation une fois ; retourne l'état persisté.

        Ne jamais passer ici les champs d'ordre proposés par A. Le seul
        paramètre est la référence d'une autorisation enregistrée par review().
        """
        row = self.tracker.order(authorization_id)
        if row["state"] != "RESERVED":
            return row
        auth = Authorization(**row["authorization"])
        a = AnalysisEnvelope.from_dict(self.tracker.analysis(auth.analysis_id))
        try:
            market, portfolio = self._snapshots(auth.market_id)
        except Exception as exc:
            with self.tracker.transaction() as db:
                self.tracker.event("PRE_SEND_REFRESH_FAILED", authorization_id, {"error_type": type(exc).__name__}, db)
            return self.tracker.order(authorization_id)  # aucun envoi ; réservation conservée

        # Un plan optimal à t0 ne garantit pas sa valeur à t1. Pour un ordre
        # de lot, re-prévisualiser le même montant ; ne pas dégrader la valeur
        # qui a servi à l'allocation ni modifier silencieusement l'ordre.
        batch_quote_ok = True
        if auth.batch_id is not None:
            try:
                with self.tracker.connection() as db:
                    current_assessment = self._assess(a, market, portfolio, db, exclude=authorization_id)
                if not current_assessment.eligible:
                    batch_quote_ok = False
                else:
                    q = self._call_b("PRE_SEND_PREVIEW", self.broker.preview_order,
                        links={"authorization_id": authorization_id, "analysis_id": a.analysis_id,
                               "market_id": a.market_id, "batch_id": auth.batch_id},
                        market_id=a.market_id, outcome=auth.outcome, max_total_debit=auth.max_total_debit,
                        limit_probability=auth.limit_probability, order_expires_at=auth.order_expires_at)
                    gain = self._validate_batch_quote(q, a, current_assessment, market,
                                                      auth.max_total_debit, auth.order_expires_at)
                    batch_quote_ok = (q.executable and q.amount == auth.amount
                                      and auth.expected_gain is not None
                                      and gain + EPS >= auth.expected_gain
                                      and gain > self.batch_policy.min_expected_gain)
            except Exception:
                batch_quote_ok = False

        with self.tracker.transaction() as db:
            now = self.tracker.clock()
            self.tracker.expire_unsent(now, db)
            row = self.tracker.order(authorization_id, db)
            if row["state"] != "RESERVED":
                return row
            try:
                assessment = self._assess(a, market, portfolio, db, exclude=authorization_id)
                safe = (batch_quote_ok and assessment.eligible and auth.max_total_debit in assessment.debit_tiers
                        and auth.policy_hash == self.policy.policy_hash
                        and (auth.batch_id is None or self.tracker.meta("batch_policy_hash", db) == fingerprint(self.batch_policy))
                        and auth.account_id == portfolio.account_id
                        and auth.conditions_hash == market.conditions_hash
                        and auth.limit_probability == assessment.limit_probability
                        and now < min(auth.valid_until, auth.order_expires_at))
            except (ValueError, TypeError, StateConflict):
                safe = False
            if not safe:
                db.execute("UPDATE d_orders SET state='CANCELLED',remaining=0 WHERE id=?", (authorization_id,))
                self.tracker.event("CANCELLED_BEFORE_SEND", authorization_id, {"reason": "FINAL_RECHECK_FAILED"}, db)
                return self.tracker.order(authorization_id, db)
            db.execute("UPDATE d_orders SET state='SENDING' WHERE id=? AND state='RESERVED'", (authorization_id,))
            self.tracker.event("SENDING", authorization_id, {}, db)

        try:
            report = self._call_b("PLACE_ORDER", self.broker.place_order, auth,
                                  links={"authorization_id": authorization_id, "analysis_id": auth.analysis_id,
                                         "market_id": auth.market_id, "batch_id": auth.batch_id})
            self.tracker.apply_execution(authorization_id, report)
        except Exception as exc:
            # Même une réponse mal formée peut suivre une mise réelle.
            self.tracker.unknown(authorization_id, type(exc).__name__)
        return self.tracker.order(authorization_id)

    def reconcile(self, authorization_id: str) -> dict:
        """Interroger B, jamais renvoyer la mise. None laisse la réservation."""
        row = self.tracker.order(authorization_id)
        if row["state"] not in ("SENDING", "UNKNOWN", "OPEN", "PARTIAL"):
            return row
        try:
            report = self._call_b("LOOKUP_ORDER", self.broker.lookup_order, Authorization(**row["authorization"]),
                                  links={"authorization_id": authorization_id, "analysis_id": row["analysis_id"],
                                         "market_id": row["market_id"], "batch_id": row["authorization"].get("batch_id")})
            if report is not None:
                self.tracker.apply_execution(authorization_id, report)
        except Exception as exc:
            self.tracker.unknown(authorization_id, f"RECONCILIATION_{type(exc).__name__}")
        return self.tracker.order(authorization_id)

    def recover_pending(self) -> list[dict]:
        """À appeler au démarrage. Aucun ordre n'est envoyé par cette méthode."""
        with self.tracker.transaction() as db:
            self.tracker.expire_unsent(self.tracker.clock(), db)
        return [self.reconcile(r["id"]) for r in self.tracker.orders()
                if r["state"] in ("SENDING", "UNKNOWN", "OPEN", "PARTIAL")]


class Pipeline:
    """Une itération du produit ; aucun thread ni boucle autonome cachée.

    analyst doit avoir la signature analyse_market(market, *, budget).
    Une version de modèle/prompts différente doit changer agent_revision.
    """

    def __init__(self, gate: GateService, budget: BudgetEngine, cache: Cache):
        if not (gate.tracker.path == budget.tracker.path == cache.tracker.path):
            raise ValueError("Gate, budget et cache doivent partager la même base")
        self.gate, self.budget, self.cache = gate, budget, cache

    def analyse(self, market_id: str, analyst: Callable[..., Any], *, cycle_id: str,
                agent_revision: str) -> AnalysisEnvelope:
        with self.gate.tracker.log_context(batch_id=cycle_id, market_id=market_id):
            return self._analyse(market_id, analyst, cycle_id=cycle_id, agent_revision=agent_revision)

    def _analyse(self, market_id: str, analyst: Callable[..., Any], *, cycle_id: str,
                 agent_revision: str) -> AnalysisEnvelope:
        from agent.schemas import Market  # contrat existant du dépôt, non remplacé

        if not agent_revision:
            raise ValueError("Version de l'agent obligatoire pour le cache")
        market = self.gate.fetch_market(market_id)
        market.validate()
        now = self.gate.tracker.clock()
        if (market.market_id != market_id or market.token != "MANA" or market.outcome_type != "BINARY"
                or market.mechanism != "cpmm-1" or market.is_closed or market.is_resolved
                or market.close_time is None or now >= market.close_time
                or not 0 <= now - market.fetched_at <= self.gate.policy.max_market_age):
            raise ValueError("Marché inéligible ou snapshot périmé avant analyse")
        key = self.cache.key(agent_revision, market_id, market.conditions_hash)
        cached = self.cache.get("analysis", key)
        if cached:
            try:
                a = AnalysisEnvelope.from_dict(cached.value)
                a.validate()
                if (a.market_id == market_id and a.conditions_hash == market.conditions_hash
                        and 0 <= now - a.reference_at <= self.gate.policy.max_analysis_age
                        and abs(a.market_probability - market.probability) <= self.gate.policy.max_market_move):
                    # Vérification de l'identité immuable ; aucune nouvelle date.
                    self.gate.tracker.record_analysis(a)
                    self.gate.tracker.log_event("D", "ANALYSIS_CACHE_REUSED", analysis_id=a.analysis_id,
                        market_id=market_id, batch_id=cycle_id,
                        payload={"reference_at": a.reference_at, "completed_at": a.completed_at})
                    return a
            except (ValueError, TypeError, KeyError):
                self.cache.invalidate("analysis", key)
        analysis_id = uuid4().hex
        with self.budget.begin(analysis_id=analysis_id, market_id=market_id, cycle_id=cycle_id) as session:
            with self.gate.tracker.operation("A", "ANALYSE_MARKET", analysis_id=analysis_id,
                                            market_id=market_id, batch_id=cycle_id,
                                            payload={"reference_at": market.fetched_at,
                                                     "agent_revision": agent_revision}):
                result = analyst(Market(id=market_id, question=market.question,
                                        description=market.description, probability=market.probability,
                                        close_time=market.close_time, fetched_at=market.fetched_at), budget=session)
            raw = plain(result)
            if not isinstance(raw, dict):
                raise ValueError("A doit retourner AgentAnalysis")
            if abs(finite(raw["market_probability"], "market_probability") - market.probability) > EPS:
                raise ValueError("A a changé la probabilité de marché observée")
            completed = self.gate.tracker.clock()
            trace = self.budget.trace(analysis_id)
            a = AnalysisEnvelope(
                analysis_id, market_id, market.fetched_at, completed, market.conditions_hash,
                market.probability, raw["initial_probability"], raw["final_probability"],
                raw["confidence"], raw["decision"],
                tuple(EvidenceRecord(**e) for e in raw["evidence"]), raw["reasoning"],
                trace["urls"], trace["critic_completed"], analysis_id)
            a.validate()
        self.gate.tracker.record_analysis(a)
        self.cache.put("analysis", key, a, created_at=a.reference_at, ttl=self.gate.policy.max_analysis_age)
        return a

    def run_cycle(self, market_ids: list[str], analyst: Callable[..., Any], *, cycle_id: str,
                  agent_revision: str, execute: bool = False) -> list[dict]:
        """Collecter jusqu'à dix analyses, puis allouer le lot en UNE décision.

        Le budget et le délai peuvent produire moins de dix dossiers. Pas de
        boucle d'attente pour compléter un lot ; un appel commencé finit sous
        son timeout propre. execute=False reste le défaut. Les mises du lot
        sont envoyées séparément et peuvent échouer/expirer indépendamment.
        """
        if not cycle_id or not agent_revision:
            raise ValueError("cycle_id et agent_revision obligatoires")
        requested = list(dict.fromkeys(market_ids))
        limit = min(self.gate.batch_policy.max_analyses, self.budget.policy.analyses_per_cycle)
        unique = requested[:limit]
        tracker = self.gate.tracker
        with tracker.transaction() as db:
            # Un cycle déjà nommé ne peut silencieusement désigner un autre travail.
            tracker.bind("batch_request:" + cycle_id,
                         fingerprint({"markets": sorted(requested), "agent_revision": agent_revision}), db)
            old = tracker.batch(cycle_id, db)
        if old:
            return self._cycle_results(self.gate._replay_batch(old["payload"]), execute=execute)
        collected = []
        errors = [{"market_id": mid, "error": "BATCH_LIMIT", "reason": "Non commencé : plafond de taille/budget du cycle"}
                  for mid in requested[limit:]]
        with tracker.operation("D", "CYCLE", batch_id=cycle_id,
                               payload={"requested_markets": requested, "maximum": limit}) as log:
            collection_started = tracker.clock()
            monotonic_started = time.perf_counter()
            deadline = collection_started + self.gate.batch_policy.max_collection_seconds
            if requested[limit:]:
                tracker.log_event("D", "COLLECTION_LIMIT", status="SKIPPED", payload={"deferred": requested[limit:]})
            for index, market_id in enumerate(unique):
                if (tracker.clock() >= deadline or tracker.clock() < collection_started
                        or time.perf_counter()-monotonic_started >= self.gate.batch_policy.max_collection_seconds):
                    for deferred in unique[index:]:
                        errors.append({"market_id": deferred, "error": "COLLECTION_DEADLINE",
                                       "reason": "Non commencé : fenêtre de collecte terminée"})
                    tracker.log_event("D", "COLLECTION_CLOSED", status="SKIPPED",
                                      payload={"cause": "DEADLINE", "deferred": unique[index:]})
                    break
                try:
                    with tracker.log_context(market_id=market_id):
                        collected.append(self.analyse(market_id, analyst, cycle_id=cycle_id,
                                                      agent_revision=agent_revision))
                except (BudgetExceeded, ValueError, TypeError, StateConflict) as exc:
                    errors.append({"market_id": market_id, "error": type(exc).__name__,
                                   "reason": getattr(exc, "code", "ANALYSIS_OR_INPUT_FAILURE")})
                    tracker.log_event("D", "ANALYSIS_UNAVAILABLE", status="FAILED", market_id=market_id,
                                      payload={"error_type": type(exc).__name__, "code": getattr(exc, "code", None)})
                except Exception as exc:
                    errors.append({"market_id": market_id, "error": type(exc).__name__, "reason": "ANALYSIS_OR_NETWORK_FAILURE"})
                    tracker.log_event("D", "ANALYSIS_UNAVAILABLE", status="FAILED", market_id=market_id,
                                      payload={"error_type": type(exc).__name__})
            batch = self.gate.review_batch(collected, batch_id=cycle_id, collection_errors=errors)
            log.update({"collected": len(collected), "failed_or_deferred": len(errors),
                        "estimated_gain": batch.expected_gain, "reserved": batch.total_reserved})
            return self._cycle_results(batch, execute=execute, errors=errors)

    def _cycle_results(self, batch: BatchDecision, *, execute: bool, errors: list[dict] | None = None) -> list[dict]:
        if errors is None:
            saved = self.gate.tracker.batch(batch.batch_id)
            errors = saved["payload"].get("collection_errors", []) if saved else []
        results = list(errors)
        for decision in batch.decisions:
            analysis = self.gate.tracker.analysis(decision.analysis_id)
            order = self.gate.execute(decision.authorization.authorization_id) if execute and decision.approved else None
            results.append({"market_id": analysis["market_id"], "batch_id": batch.batch_id,
                            "decision": plain(decision), "execution": order})
        return results
