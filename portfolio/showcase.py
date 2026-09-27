"""Presentation workflow: narrated research, deadline-aware discovery and diagnostics.

Closing within N minutes is NOT resolving or paying within N minutes.
This planner never sends live bets. Existing V1 and portfolio contracts stay intact.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import re
import time
from uuid import uuid4

import requests

from .engine import allocate
from .manifold import Manifold, TOPICS, POLITICAL, RemoteError
from .models import InvalidData, Option, Policy, Holdings, number
from .research import Researcher, BudgetExceeded


def error_info(exc) -> dict:
    """Useful diagnostics without response bodies, credentials or signed URLs."""
    status = getattr(exc, 'status_code', None)
    if status is None and getattr(exc, 'response', None) is not None:
        status = getattr(exc.response, 'status_code', None)
    code = getattr(exc, 'code', None)
    if not isinstance(code, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', code):
        code = type(exc).__name__
    reasons = {401: 'Clé API refusée : vérifier le fichier .env local.',
               403: 'Accès refusé : permissions, modèle ou solde à vérifier.',
               404: 'Ressource ou modèle inaccessible : vérifier le modèle configuré.',
               429: 'Quota/crédit ou limite de débit du fournisseur atteint.',
               400: 'Le fournisseur refuse les paramètres de la requête.'}
    if isinstance(exc, (InvalidData, BudgetExceeded, RemoteError)):
        message = str(exc)
    elif isinstance(exc, (TimeoutError, requests.Timeout)) or 'Timeout' in type(exc).__name__:
        message = 'Délai de réponse dépassé ; ce n’est pas un edge nul.'
    else:
        message = reasons.get(status, 'Appel interrompu ; voir le code technique, pas une décision de marché.')
    return dict(code=code, http_status=status, reason=message)


class HorizonManifold(Manifold):
    def __init__(self, *, horizon_minutes=None, **kwargs):
        super().__init__(**kwargs)
        self.horizon_minutes = None if horizon_minutes is None else number(horizon_minutes, 'horizon_minutes', 1, 525600)
        self.cutoff = None if self.horizon_minutes is None else self.clock() + self.horizon_minutes * 60

    def fetch(self, market_id, domain, family=None):
        market = super().fetch(market_id, domain, family)
        if self.cutoff is not None and market.close_time > self.cutoff:
            raise InvalidData('Clôture au-delà de l’horizon choisi ; résolution/paiement non garantis')
        return market

    def discover(self, domains, *, limit=8, min_volume=50, families=None, deadline_seconds=45):
        if self.cutoff is None:
            return super().discover(domains, limit=limit, min_volume=min_volume,
                                    families=families, deadline_seconds=deadline_seconds)
        if not domains or any(d not in TOPICS for d in domains) or not 1 <= limit <= 10:
            raise InvalidData('Domaines ou taille de panier invalides')
        number(min_volume, 'min_volume')
        deadline = time.monotonic() + number(deadline_seconds, 'deadline_seconds', .1)
        queues, errors = [], []
        for domain in dict.fromkeys(domains):
            for kind in ('BINARY', 'MULTIPLE_CHOICE'):
                if time.monotonic() >= deadline:
                    break
                try:
                    # Documented sort; volume-first misses short-horizon markets.
                    rows = self._request('GET', '/search-markets', params={
                        'filter': 'open', 'contractType': kind, 'topicSlug': TOPICS[domain],
                        'sort': 'close-date', 'limit': 100})
                    if not isinstance(rows, list):
                        raise InvalidData('Réponse de recherche invalide')
                    queues.append((domain, deque(rows)))
                except Exception as exc:
                    errors.append(dict(stage='collecte', domain=domain, **error_info(exc)))
        result, seen, inspected = [], set(), 0
        while any(q for _, q in queues) and len(result) < limit and time.monotonic() < deadline and inspected < 48:
            for domain, q in queues:
                if not q or len(result) >= limit or inspected >= 48 or time.monotonic() >= deadline:
                    continue
                raw = q.popleft()
                mid = raw.get('id')
                if not isinstance(mid, str) or mid in seen:
                    continue
                seen.add(mid)
                try:
                    closes = number(raw.get('closeTime'), 'closeTime') / 1000
                    if not self.clock() < closes <= self.cutoff:
                        errors.append(dict(stage='collecte', market_id=mid, code='OUTSIDE_HORIZON',
                                           reason='Hors horizon de clôture', close_time=closes))
                        continue
                    if POLITICAL.search(raw.get('question', '')):
                        raise InvalidData('Politique hors périmètre')
                    inspected += 1
                    market = self.fetch(mid, domain, (families or {}).get(mid))
                    if market.volume_24h < min_volume:
                        raise InvalidData('Volume récent inférieur au seuil configuré')
                    result.append(market)
                except Exception as exc:
                    errors.append(dict(stage='collecte', market_id=mid, **error_info(exc)))
        if not result:
            errors.append(dict(stage='collecte', code='NO_MARKET_WITHIN_HORIZON',
                reason='Aucun marché compatible trouvé dans l’horizon demandé. Aucun appel IA lancé. Élargir explicitement la durée ou les domaines.'))
        return result, errors


class NarratedResearcher(Researcher):
    """Retain BOTH public explanations; callbacks describe completed API phases."""
    def __init__(self, *args, progress=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress = progress
        self.phase_reports = {}

    def _call(self, market, run, phase, previous=None, deadline=None):
        start = time.monotonic()
        if self.progress:
            self.progress(dict(stage=phase, status='STARTED', market_id=market.id))
        try:
            result = super()._call(market, run, phase, previous, deadline)
        except Exception as exc:
            self.phase_reports.setdefault(market.id, {})[phase] = dict(
                status='FAILED', duration_seconds=time.monotonic()-start, **error_info(exc))
            if self.progress:
                self.progress(dict(stage=phase, status='FAILED', market_id=market.id, **error_info(exc)))
            raise
        public = dict(status='DONE', reasoning=result.reasoning, probabilities=result.final,
                      confidence=result.confidence, evidence=[asdict(e) for e in result.evidence],
                      duration_seconds=time.monotonic()-start)
        self.phase_reports.setdefault(market.id, {})[phase] = public
        if self.progress:
            self.progress(dict(stage=phase, status='DONE', market_id=market.id))
        return result


def run_showcase(markets, researcher, source, *, policy=Policy(), store=None,
                 seconds=180, clock=time.time, progress=None):
    """Reserve preview time, preserve small quotes if another tier fails, explain every skip."""
    if len(markets) > policy.max_markets or len({m.id for m in markets}) != len(markets):
        raise InvalidData('Panier trop grand ou dupliqué')
    duration = number(seconds, 'seconds', 5, 1800)
    run, start = uuid4().hex, time.monotonic()
    deadline = start + duration
    preview_reserve = min(duration * .4, max(20, 12 * len(markets)))
    research_deadline = deadline - preview_reserve
    analyses, options, errors, diagnostics, events = [], [], [], [], []

    def emit(stage, mid='', status='STARTED', **extra):
        row = dict(stage=stage, market_id=mid, status=status,
                   elapsed_seconds=round(time.monotonic()-start, 3), **extra)
        events.append(row)
        if progress:
            progress(row)

    if isinstance(researcher, NarratedResearcher):
        researcher.progress = lambda row: emit(**row)
    for market in markets:
        if time.monotonic() >= research_deadline:
            errors.append(dict(market_id=market.id, code='RESEARCH_DEADLINE',
                reason='Non analysé : temps réservé aux prévisualisations des dossiers déjà terminés.'))
            continue
        try:
            emit('Analyse du marché', market.id)
            analysis = researcher.analyse(market, run, deadline=research_deadline)
            analysis.validate(market)
            analyses.append((market, analysis))
            emit('Analyse du marché', market.id, 'DONE', source=analysis.source)
        except Exception as exc:
            errors.append(dict(market_id=market.id, stage='analyse', **error_info(exc)))
            emit('Analyse du marché', market.id, 'FAILED', **error_info(exc))
    for market, analysis in analyses:
        try:
            if time.monotonic() >= deadline:
                raise InvalidData('Temps du cycle épuisé avant prévisualisation')
            if not 0 <= clock() - analysis.completed_at <= policy.max_age:
                raise InvalidData('Prévision trop ancienne')
            if analysis.confidence == 'low' or not analysis.evidence:
                raise InvalidData('Confiance faible ou aucune preuve traçable ; refus explicite')
            emit('Risque et prévisualisation', market.id)
            fresh = source.fetch(market.id, market.domain, market.family)
            if fresh.conditions_hash != analysis.conditions_hash:
                raise InvalidData('Critères ou réponses modifiés depuis l’analyse')
            old = {a.id: a.probability for a in market.answers}
            legs = []
            for answer in fresh.answers:
                for side in ('YES', 'NO'):
                    p = analysis.final[answer.id] if side == 'YES' else 1-analysis.final[answer.id]
                    price = answer.probability if side == 'YES' else 1-answer.probability
                    row = dict(market_id=market.id, question=market.question, answer_id=answer.id,
                        answer=answer.label, side=side, agent_probability=p, market_probability=price,
                        edge_points=100*(p-price), threshold_points=100*policy.min_edge)
                    if abs(answer.probability-old[answer.id]) > policy.max_price_move:
                        row['reason'] = 'Prix trop modifié depuis observation'
                    elif not 0 < price < 1 or p-price + 1e-12 < policy.min_edge:
                        row['reason'] = 'Écart inférieur au seuil (pas une erreur API)'
                    else:
                        row['reason'] = 'Candidat à prévisualiser'
                        legs.append((p/price-1, answer, side, p))
                    diagnostics.append(row)
            legs.sort(key=lambda item: (-item[0], item[1].id, item[2]))
            tiers = sorted(set(policy.tiers))
            if analysis.confidence == 'medium' or not analysis.critic_completed:
                tiers = tiers[:1]
            for _, answer, side, p in legs[:2]:
                raw_limit = analysis.final[answer.id] + (policy.min_edge if side == 'NO' else -policy.min_edge)
                rounding = ROUND_CEILING if side == 'NO' else ROUND_FLOOR
                limit = float(Decimal(str(raw_limit)).quantize(Decimal('.01'), rounding=rounding))
                if not .01 <= limit <= .99:
                    continue
                for amount in tiers:
                    if time.monotonic() >= deadline:
                        errors.append(dict(market_id=market.id, reason='Délai atteint entre deux prévisualisations'))
                        break
                    detail = dict(market_id=market.id, answer_id=answer.id, answer=answer.label, side=side, amount=amount)
                    try:
                        quote = source.preview(fresh, answer.id, side, amount, limit)
                        if (quote.market_id, quote.answer_id, quote.side) != (market.id, answer.id, side):
                            raise InvalidData('Prévisualisation d’un autre contrat')
                        if not quote.full_fill or abs(quote.amount-amount) > 1e-6:
                            raise InvalidData('Remplissage complet non confirmé à cette taille')
                        debit = number(quote.estimated_debit, 'estimated_debit', amount)
                        shares = number(quote.shares, 'shares', .000001)
                        before = number(quote.probability_before, 'probBefore', 0, 1)
                        price = before if side == 'YES' else 1-before
                        if abs(before-answer.probability) > policy.max_price_move or p-price + 1e-12 < policy.min_edge:
                            raise InvalidData('Prix actualisé hors marge autorisée')
                        gain = p*shares-debit
                        detail.update(estimated_debit=debit, shares=shares, expected_profit=gain)
                        if gain <= 0:
                            raise InvalidData('Gain estimé non positif après frais/réserve')
                        if debit > policy.max_market:
                            raise InvalidData('Débit frais compris au-dessus du plafond par marché')
                        options.append(Option(f'{market.id}:{answer.id}:{side}:{amount}', market.id,
                            market.question, answer.id, answer.label, side, market.domain, market.family,
                            amount, debit, shares, p, price, gain, quote.source, quote.quoted_at))
                        detail['reason'] = 'Option chiffrée recevable'
                    except Exception as exc:
                        detail.update(error_info(exc))
                    diagnostics.append(detail)
            emit('Risque et prévisualisation', market.id, 'DONE')
        except Exception as exc:
            errors.append(dict(market_id=market.id, stage='risque', **error_info(exc)))
            emit('Risque et prévisualisation', market.id, 'FAILED', **error_info(exc))
    now = clock()
    valid = []
    by_market = {m.id: m for m in markets}
    for o in options:
        if not 0 <= now-o.quoted_at <= policy.max_quote_age or by_market[o.market_id].close_time <= now:
            diagnostics.append(dict(market_id=o.market_id, amount=o.amount,
                                    reason='Prévisualisation périmée ou marché fermé avant allocation'))
        else:
            valid.append(o)
    emit('Allocation globale')
    plan = allocate(valid, policy)
    chosen = {o.market_id for o in plan.selected}
    for m, _ in analyses:
        if m.id not in chosen and not any(e['market_id'] == m.id for e in errors):
            available = any(o.market_id == m.id for o in valid)
            errors.append(dict(market_id=m.id, code='NOT_SELECTED' if available else 'NO_VALID_OPTION',
                reason='Option admissible mais non retenue sous les plafonds du panier' if available else
                       'Aucune option retenable : consulter les diagnostics prix, frais et remplissage'))
    emit('Allocation globale', status='DONE', positions=len(plan.selected))
    phase_reports = getattr(researcher, 'phase_reports', {})
    report = dict(run_id=run, mode='PREVIEW_ONLY', model=getattr(researcher, 'model', 'SYNTHETIC_DEMO'),
        markets=[asdict(m) for m in markets], analyses=[asdict(a) for _, a in analyses],
        options=[asdict(o) for o in valid], plan=asdict(plan), policy=asdict(policy), holdings=asdict(Holdings()),
        errors=errors, diagnostics=diagnostics, phase_reports=phase_reports, events=events,
        duration_seconds=round(time.monotonic()-start, 3), realized_pnl_mana=None,
        created_at=clock(), research_seconds_reserved=duration-preview_reserve,
        preview_seconds_reserved=preview_reserve,
        usage=store.usage(run) if store else dict(calls=0, known_cost_usd=0, unknown_cost_calls=0,
            charged_or_reserved_usd=0, input_tokens=0, output_tokens=0, web_calls=0))
    if store:
        store.save_report(run, report)
    return report
