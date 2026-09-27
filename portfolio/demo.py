"""Deterministic synthetic fixtures. Never real forecasts or realised profits.
Run: python -m portfolio.demo --out portfolio-demo.json
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from .engine import run_cycle
from .models import Analysis, Answer, Evidence, Market, Quote


def markets(clock=time.time) -> list[Market]:
    now = clock()
    specifications = [
        ('demo-ai', 'AI', 'BINARY', [('YES', 'YES', .40)]),
        ('demo-space', 'Space', 'BINARY', [('YES', 'YES', .65)]),
        ('demo-science', 'Science', 'SUM_TO_ONE', [('A', 'Résultat A', .20), ('B', 'Résultat B', .50), ('C', 'Autre résultat', .30)]),
        ('demo-crypto', 'Crypto', 'INDEPENDENT', [('X', 'Étape technique X', .30), ('Y', 'Étape technique Y', .60)]),
        ('demo-no-edge', 'AI', 'BINARY', [('YES', 'YES', .50)]),
    ]
    return [Market(mid, f'DÉMO SYNTHÉTIQUE — {mid}', 'Critères fictifs réservés aux tests, pas un marché réel.',
                   domain, domain, kind, tuple(Answer(*a) for a in answers), now + 86400, now, 500)
            for mid, domain, kind, answers in specifications]


class DemoSource:
    def __init__(self, values, *, clock=time.time):
        self.values = {m.id: m for m in values}
        self.clock = clock

    def fetch(self, market_id, domain, family=None):
        return replace(self.values[market_id], fetched_at=self.clock())

    def preview(self, market, answer_id, side, amount, limit_probability):
        answer = next(a for a in market.answers if a.id == answer_id)
        price = answer.probability if side == 'YES' else 1-answer.probability
        # Deliberately synthetic slippage; not an implementation of Manifold AMM.
        shares = amount / (price + amount * .0005)
        return Quote(market.id, answer_id, side, amount, amount, shares,
                     answer.probability, self.clock(), 'SYNTHETIC_DEMO', True)


class DemoResearcher:
    def __init__(self, *, clock=time.time):
        self.clock = clock

    def analyse(self, market, run, *, deadline=None):
        probabilities = {
            'demo-ai': {'YES': .60}, 'demo-space': {'YES': .40},
            'demo-science': {'A': .45, 'B': .30, 'C': .25},
            'demo-crypto': {'X': .52, 'Y': .61}, 'demo-no-edge': {'YES': .50},
        }[market.id]
        evidence = (Evidence('Fixture de test', 'https://example.org/fixture',
                             'Donnée fabriquée pour tester le logiciel, pas une source factuelle.', 'fixture'),)
        return Analysis(market.id, market.conditions_hash, probabilities, probabilities,
                        'high', evidence, 'Prévision synthétique de démonstration.', True,
                        self.clock(), source='SYNTHETIC_DEMO')


def build_demo(policy=None, domains=None):
    from .models import Policy
    policy = policy or Policy()
    values = [m for m in markets() if domains is None or m.domain in domains][:policy.max_markets]
    report = run_cycle(values, DemoResearcher(), DemoSource(values), policy=policy)
    report['data_origin'] = 'SYNTHETIC_DEMO'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', help='Optional JSON file; never contains API keys')
    args = parser.parse_args()
    report = build_demo()
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        Path(args.out).write_text(output, encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
