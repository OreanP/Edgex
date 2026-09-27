"""Public discovery + authenticated dry-runs ONLY. No live-order method exists.

API contracts: https://docs.manifold.markets/api and Manifold's place-bet.ts.
A fee buffer is an estimate for planning, NOT a provider debit guarantee.
"""
from __future__ import annotations

from collections import deque
import os
import re
import time
from urllib.parse import quote as urlquote

import requests

from .models import Answer, InvalidData, Market, Quote, number, text

TOPICS = {'AI': 'ai', 'Science': 'science-default', 'Space': 'space', 'Crypto': 'crypto-speculation'}
POLITICAL = re.compile(
    r'\b(politic\w*|election\w*|electoral|ballot\w*|midterms?|presiden\w*|parliament\w*|'
    r'congress\w*|senat\w*|prime minister|gouvernement|referendum|référendum|'
    r'democrat\w*|republican\w*|trump|biden|harris|macron|legislation|législation|'
    r'campaign|politique\w*)\b', re.I)


class RemoteError(RuntimeError):
    pass


def _plain(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if isinstance(node.get('text'), str):
            return node['text']
        parts = [_plain(n) for n in node.get('content', [])]
        return ('\n' if node.get('type') in ('doc', 'bulletList', 'orderedList') else '').join(parts)
    return ''


def normalize(raw: dict, domain: str, now: float, family: str | None = None) -> Market:
    if domain not in TOPICS:
        raise InvalidData('Domaine hors périmètre')
    if raw.get('token') != 'MANA':
        raise InvalidData('Token absent ou différent de MANA')
    if raw.get('isResolved') is not False or raw.get('isClosed', False) is not False:
        raise InvalidData('Statut ouvert non confirmé')
    question = text(raw.get('question'), 'question')
    description = text(raw.get('textDescription') or _plain(raw.get('description')), 'critères de résolution')
    scope = ' '.join((question, description, ' '.join(raw.get('groupSlugs') or [])))
    if POLITICAL.search(scope):
        raise InvalidData('Marché politique hors périmètre')
    close = number(raw.get('closeTime'), 'closeTime') / 1000
    if close <= now:
        raise InvalidData('Marché fermé')
    kind = raw.get('outcomeType')
    if kind == 'BINARY' and raw.get('mechanism') == 'cpmm-1':
        answers = (Answer('YES', 'YES', number(raw.get('probability'), 'probability', 0, 1)),)
    elif kind == 'MULTIPLE_CHOICE' and raw.get('mechanism') == 'cpmm-multi-1':
        if not isinstance(raw.get('shouldAnswersSumToOne'), bool):
            raise InvalidData('Sémantique multi-réponses inconnue')
        kind = 'SUM_TO_ONE' if raw['shouldAnswersSumToOne'] else 'INDEPENDENT'
        data = raw.get('answers')
        if not isinstance(data, list) or not 2 <= len(data) <= 8:
            raise InvalidData('La V2 accepte 2 à 8 réponses complètes')
        if any(a.get('resolution') is not None for a in data):
            raise InvalidData('Résolution partielle non prise en charge')
        answers = tuple(Answer(text(a.get('id'), 'answer.id'), text(a.get('text'), 'answer.text'),
                               number(a.get('prob', a.get('probability')), 'answer.prob', 0, 1)) for a in data)
    else:
        raise InvalidData('Type/mécanisme non supporté')
    return Market(text(raw.get('id'), 'id'), question, description, domain, family or domain,
                  kind, answers, close, now, number(raw.get('volume24Hours', 0), 'volume24Hours'),
                  raw.get('url') or '')


class Manifold:
    """Bounded requests, no retries. A key is needed only for preview()."""
    def __init__(self, *, api_key: str | None = None, session=None, clock=time.time,
                 max_requests: int = 100, fee_buffer: float = 1.0):
        self.session = session or requests.Session()
        self.api_key = api_key if api_key is not None else os.getenv('MANIFOLD_API_KEY', '')
        self.clock = clock
        self.max_requests = max_requests
        self.requests = 0
        self.fee_buffer = number(fee_buffer, 'fee_buffer')

    def _request(self, method: str, path: str, **kwargs):
        if self.requests >= self.max_requests:
            raise RemoteError('Plafond de requêtes Manifold atteint')
        self.requests += 1
        response = self.session.request(method, 'https://api.manifold.markets/v0' + path,
                                        timeout=(3.05, 10), **kwargs)
        response.raise_for_status()
        return response.json()

    def fetch(self, market_id: str, domain: str, family: str | None = None) -> Market:
        raw = self._request('GET', '/market/' + urlquote(text(market_id, 'id'), safe=''))
        return normalize(raw, domain, self.clock(), family)

    def discover(self, domains: list[str], *, limit: int = 8, min_volume: float = 50,
                 families: dict[str, str] | None = None, deadline_seconds: float = 45):
        if not domains or any(d not in TOPICS for d in domains) or not 1 <= limit <= 10:
            raise InvalidData('Choisir 1 à 4 domaines et 1 à 10 marchés')
        number(min_volume, 'min_volume')
        deadline = time.monotonic() + number(deadline_seconds, 'deadline_seconds', .1)
        queues, errors = [], []
        for domain in dict.fromkeys(domains):
            for kind in ('BINARY', 'MULTIPLE_CHOICE'):
                if time.monotonic() >= deadline:
                    errors.append({'stage': 'discovery', 'reason': 'Délai de collecte atteint'})
                    break
                try:
                    rows = self._request('GET', '/search-markets', params={
                        'filter': 'open', 'contractType': kind, 'topicSlug': TOPICS[domain],
                        'sort': '24-hour-vol', 'limit': 12})
                    if not isinstance(rows, list):
                        raise InvalidData('Réponse search-markets invalide')
                    queues.append((domain, deque(rows)))
                except (requests.RequestException, ValueError, RemoteError) as exc:
                    errors.append({'stage': 'discovery', 'domain': domain, 'reason': type(exc).__name__})
        markets, seen, inspected = [], set(), 0
        # Round-robin across domains AND kinds, not just the most active topic.
        while any(q for _, q in queues) and len(markets) < limit and inspected < 32:
            if time.monotonic() >= deadline:
                errors.append({'stage': 'discovery', 'reason': 'Délai de collecte atteint'})
                break
            for domain, queue in queues:
                if not queue or len(markets) >= limit or inspected >= 32 or time.monotonic() >= deadline:
                    continue
                raw = queue.popleft()
                mid = raw.get('id')
                if not isinstance(mid, str) or mid in seen:
                    continue
                seen.add(mid)
                inspected += 1
                try:
                    if POLITICAL.search(raw.get('question', '')):
                        raise InvalidData('Marché politique hors périmètre')
                    market = self.fetch(mid, domain, (families or {}).get(mid))
                    if market.volume_24h < min_volume:
                        raise InvalidData('Activité récente insuffisante')
                    markets.append(market)
                except (requests.RequestException, ValueError, RemoteError) as exc:
                    reason = str(exc) if isinstance(exc, InvalidData) else type(exc).__name__
                    errors.append({'stage': 'discovery', 'market_id': mid, 'reason': reason})
        return markets, errors

    def preview(self, market: Market, answer_id: str, side: str, amount: float,
                limit_probability: float) -> Quote:
        if not self.api_key:
            raise RemoteError('MANIFOLD_API_KEY requise pour le dry-run ; aucune mise réelle')
        if side not in ('YES', 'NO') or answer_id not in {a.id for a in market.answers}:
            raise InvalidData('Sens/réponse inconnue')
        number(amount, 'amount', 1, 1000)
        number(limit_probability, 'limit_probability', .01, .99)
        payload = {'contractId': market.id, 'outcome': side, 'amount': amount,
                   'dryRun': True, 'limitProb': limit_probability, 'expiresMillisAfter': 30_000}
        if market.kind != 'BINARY':
            payload['answerId'] = answer_id
        raw = self._request('POST', '/bet', headers={'Authorization': f'Key {self.api_key}'}, json=payload)
        if raw.get('betId') != 'dry-run' or raw.get('contractId') != market.id or raw.get('outcome') != side:
            raise InvalidData('Réponse inattendue : simulation non confirmée')
        if market.kind != 'BINARY' and raw.get('answerId') != answer_id:
            raise InvalidData('La simulation porte sur une autre réponse')
        filled = number(raw.get('amount'), 'filled')
        shares = number(raw.get('shares'), 'shares')
        fees = raw.get('fees') or {}
        reported_fees = sum(number(v, 'fee') for v in fees.values())
        # Conservative planning estimate; may double-count fees already included
        # in amount. This is NOT actual account P&L or a guaranteed future bound.
        estimated = filled + reported_fees + self.fee_buffer
        full = raw.get('isFilled') is True and abs(filled - amount) <= 1e-6
        return Quote(market.id, answer_id, side, amount, estimated, shares,
                     number(raw.get('probBefore'), 'probBefore', 0, 1), self.clock(),
                     'manifold-dry-run (frais estimés + réserve)', full)
