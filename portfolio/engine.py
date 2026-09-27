"""Global, bounded multiple-choice knapsack for PREVIEW portfolios.

Generalizes the existing D allocate_batch idea: at most one leg/size per parent
market. Limits apply to the whole basket, domains and operator-defined families.
It does not estimate correlations, rebalance existing holdings or place orders.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import time
from uuid import uuid4

from .models import Analysis, Holdings, InvalidData, Market, Option, Plan, Policy, number


def _debit(value: float) -> int:
    return int((Decimal(str(number(value, 'debit'))) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _room(value: float) -> int:
    return max(0, int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR)))


def allocate(options: list[Option], policy: Policy, holdings: Holdings = Holdings()) -> Plan:
    """Maximize estimated profit within hard planning caps; cash is an option.

    Bounded search reports optimal=False if interrupted. No silent claim of
    global optimality beyond the supplied, pre-screened options and estimates.
    """
    number(holdings.total, 'holdings.total')
    for mapping in (holdings.by_market, holdings.by_domain, holdings.by_family):
        for value in mapping.values():
            number(value, 'exposure')
        if sum(mapping.values()) > holdings.total + 1e-6:
            raise InvalidData('Expositions incompatibles avec le total existant')
    groups, ids, metadata = {}, set(), {}
    for option in options:
        if option.id in ids:
            raise InvalidData('Option dupliquée')
        ids.add(option.id)
        signature = option.domain, option.family
        if option.market_id in metadata and metadata[option.market_id] != signature:
            raise InvalidData('Famille/domaine incohérent pour le même marché')
        metadata[option.market_id] = signature
        number(option.expected_profit, 'expected_profit', -1e12, 1e12)
        number(option.estimated_debit, 'estimated_debit', .000001)
        number(option.agent_probability, 'agent_probability', 0, 1)
        number(option.market_probability, 'market_probability', 0, 1)
        number(option.shares, 'shares')
        if abs(option.expected_profit - (option.agent_probability * option.shares - option.estimated_debit)) > 1e-6:
            raise InvalidData('Gain incohérent avec coût, probabilité et parts')
        if option.side not in ('YES', 'NO'):
            raise InvalidData('Sens invalide')
        if (option.expected_profit <= 0 or holdings.by_market.get(option.market_id, 0) > 0
                or option.estimated_debit > policy.max_market):
            continue
        groups.setdefault(option.market_id, []).append(option)
    if len(groups) > policy.max_markets:
        raise InvalidData('Trop de marchés pour la recherche bornée')
    # Safe dominance: choices for the same market use the same risk buckets.
    grouped = []
    for mid, choices in sorted(groups.items()):
        choices.sort(key=lambda o: (-o.expected_profit, o.estimated_debit, o.id))
        kept = []
        for o in choices:
            if not any(k.estimated_debit <= o.estimated_debit and k.expected_profit >= o.expected_profit for k in kept):
                kept.append(o)
        grouped.append(kept)
    room = _room(min(policy.bankroll - policy.cash_reserve, policy.max_total - holdings.total))
    best, best_gain, best_cost, nodes, complete = (), 0.0, 0, 0, True
    suffix = [0.0] * (len(grouped) + 1)
    for i in range(len(grouped) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + max(o.expected_profit for o in grouped[i])
    domains, families = {}, {}

    def search(i, selected, cost, gain):
        nonlocal best, best_gain, best_cost, nodes, complete
        if nodes >= policy.max_nodes:
            complete = False
            return
        nodes += 1
        # Every partial selection is already feasible; retain it if time runs out.
        if gain > best_gain + 1e-12 or (abs(gain - best_gain) <= 1e-12 and cost < best_cost):
            best, best_gain, best_cost = tuple(selected), gain, cost
        if i == len(grouped) or gain + suffix[i] < best_gain - 1e-12:
            return
        for o in grouped[i]:
            debit = _debit(o.estimated_debit)
            d, f = domains.get(o.domain, 0), families.get(o.family, 0)
            if (cost + debit > room or
                d + debit > _room(policy.max_domain - holdings.by_domain.get(o.domain, 0)) or
                f + debit > _room(policy.max_family - holdings.by_family.get(o.family, 0))):
                continue
            domains[o.domain], families[o.family] = d + debit, f + debit
            search(i + 1, selected + [o], cost + debit, gain + o.expected_profit)
            domains[o.domain], families[o.family] = d, f
        search(i + 1, selected, cost, gain)

    search(0, [], 0, 0.0)
    spent = best_cost / 1_000_000
    return Plan(best, spent, best_gain, policy.bankroll - spent, nodes, complete)


def _options(market: Market, analysis: Analysis, source, policy: Policy,
             *, deadline: float, clock=time.time) -> list[Option]:
    analysis.validate(market)
    if analysis.confidence == 'low' or not analysis.evidence:
        raise InvalidData('Confiance faible ou aucune source traçable')
    if not 0 <= clock() - analysis.completed_at <= policy.max_age:
        raise InvalidData('Analyse périmée')
    fresh = source.fetch(market.id, market.domain, market.family)
    if fresh.conditions_hash != market.conditions_hash:
        raise InvalidData('Critères/réponses modifiés depuis l’analyse')
    old = {a.id: a.probability for a in market.answers}
    legs = []
    for answer in fresh.answers:
        if abs(answer.probability - old[answer.id]) > policy.max_price_move:
            continue
        for side in ('YES', 'NO'):
            p = analysis.final[answer.id] if side == 'YES' else 1-analysis.final[answer.id]
            price = answer.probability if side == 'YES' else 1-answer.probability
            if 0 < price < 1 and p-price >= policy.min_edge:
                legs.append((p / price - 1, answer, side, p))
    # Cheap shortlist bounds API usage. Optimality is NOT claimed over every leg.
    legs.sort(key=lambda item: (-item[0], item[1].id, item[2]))
    result = []
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
                return result
            quote = source.preview(fresh, answer.id, side, amount, limit)
            if (quote.market_id, quote.answer_id, quote.side) != (market.id, answer.id, side):
                raise InvalidData('Prévisualisation pour un autre contrat')
            if not quote.full_fill or quote.shares <= 0 or abs(quote.amount - amount) > 1e-6:
                continue
            debit = number(quote.estimated_debit, 'estimated_debit', amount)
            shares = number(quote.shares, 'shares')
            before = number(quote.probability_before, 'probBefore', 0, 1)
            price = before if side == 'YES' else 1-before
            if abs(before-answer.probability) > policy.max_price_move or p-price < policy.min_edge:
                continue
            expected = p * shares - debit
            if expected > 0:
                result.append(Option(f'{market.id}:{answer.id}:{side}:{amount}', market.id,
                    market.question, answer.id, answer.label, side, market.domain, market.family,
                    amount, debit, shares, p, price, expected, quote.source, quote.quoted_at))
    return result


def run_cycle(markets: list[Market], researcher, source, *, policy: Policy = Policy(),
              holdings: Holdings = Holdings(), store=None, seconds: float = 180,
              clock=time.time, progress=None) -> dict:
    """Plan only: this function has no execution flag or live trading path."""
    if len({m.id for m in markets}) != len(markets):
        raise InvalidData('Marchés dupliqués dans le panier')
    if len(markets) > policy.max_markets:
        raise InvalidData('Panier trop grand')
    run, start = uuid4().hex, time.monotonic()
    deadline = start + number(seconds, 'seconds', .001)
    analyses, options, errors, events = [], [], [], []

    def event(stage, mid):
        entry = dict(stage=stage, market_id=mid, elapsed_seconds=round(time.monotonic()-start, 3))
        events.append(entry)
        if progress:
            progress(entry)

    # Research first, then quote; no capital is allocated to the first arrival.
    for market in markets:
        if time.monotonic() >= deadline:
            errors.append(dict(market_id=market.id, reason='Délai atteint avant analyse'))
            continue
        try:
            event('Researcher + Critic', market.id)
            analysis = researcher.analyse(market, run, deadline=deadline)
            analysis.validate(market)
            analyses.append((market, analysis))
        except Exception as exc:
            errors.append(dict(market_id=market.id, reason=type(exc).__name__))
    for market, analysis in analyses:
        if time.monotonic() >= deadline:
            errors.append(dict(market_id=market.id, reason='Délai atteint avant prévisualisation'))
            continue
        try:
            event('Prévisualisation', market.id)
            found = _options(market, analysis, source, policy, deadline=deadline, clock=clock)
            options.extend(found)
            if not found:
                errors.append(dict(market_id=market.id, reason='Aucune option avec edge, fill complet et gain estimé positif'))
        except Exception as exc:
            errors.append(dict(market_id=market.id, reason=str(exc) if isinstance(exc, InvalidData) else type(exc).__name__))
    current = clock()
    options = [o for o in options if 0 <= current - o.quoted_at <= policy.max_quote_age]
    event('Allocation globale', '')
    plan = allocate(options, policy, holdings)
    selected = {o.market_id for o in plan.selected}
    for market, _ in analyses:
        if market.id not in selected and not any(e['market_id'] == market.id for e in errors):
            errors.append(dict(market_id=market.id, reason='Non retenu par l’allocation globale ou prévisualisation périmée'))
    report = dict(run_id=run, mode='PREVIEW_ONLY', model=getattr(researcher, 'model', 'SYNTHETIC_DEMO'), markets=[asdict(m) for m in markets],
        analyses=[asdict(a) for _, a in analyses], options=[asdict(o) for o in options],
        plan=asdict(plan), policy=asdict(policy), holdings=asdict(holdings), errors=errors, events=events,
        duration_seconds=round(time.monotonic()-start, 3), realized_pnl_mana=None,
        usage=store.usage(run) if store else dict(calls=0, known_cost_usd=0, unknown_cost_calls=0,
              charged_or_reserved_usd=0, input_tokens=0, output_tokens=0, web_calls=0))
    if store:
        store.save_report(run, report)
    return report
