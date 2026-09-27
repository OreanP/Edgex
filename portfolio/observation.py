"""Prospective PAPER positions. Only GETs and dry-run previews, never live orders.
Closing, observing and resolving are separate events. No invented price history.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from uuid import uuid4
from urllib.parse import quote

from .models import InvalidData, number


class Observation:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY, run_id TEXT UNIQUE, started REAL, ends REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS observation_points (
                observation_id TEXT, at REAL, payload TEXT);
            ''')

    def start(self, report: dict, minutes: float):
        if not report['plan']['selected']:
            raise InvalidData('Aucune position proposée à observer')
        duration = number(minutes, 'minutes', 1, 1440)*60
        now, ident = self.store.clock(), uuid4().hex
        positions = report['plan']['selected']
        if report.get('data_origin') != 'SYNTHETIC_DEMO':
            if any(not 0 <= now-p['quoted_at'] <= 120 for p in positions):
                raise InvalidData('Prévisualisations trop anciennes : reconstruire le panier avant ouverture papier')
        data = dict(positions=positions, origin=report.get('data_origin'),
                    expected_profit=report['plan']['expected_profit'],
                    label=report.get('observation_label', 'PORTEFEUILLE_PAPIER'),
                    market_ids=[p['market_id'] for p in positions])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id FROM observations WHERE run_id=?', (report['run_id'],)).fetchone()
            if old:
                return old[0]
            db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',
                       (ident, report['run_id'], now, now+duration, json.dumps(data, ensure_ascii=False)))
            rows = [dict(market_id=p['market_id'], question=p['question'], value=p['shares']*p['market_probability'],
                         pnl_marked=p['shares']*p['market_probability']-p['estimated_debit'],
                         state='PAPER_OPEN', settled=False, probability=p['market_probability']) for p in positions]
            db.execute('INSERT INTO observation_points VALUES(?,?,?)', (ident, now, json.dumps(rows)))
        return ident

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute('SELECT run_id,started,ends,payload FROM observations WHERE id=?', (ident,)).fetchone()
            points = db.execute('SELECT at,payload FROM observation_points WHERE observation_id=? ORDER BY at', (ident,)).fetchall()
        if row is None:
            raise InvalidData('Observation introuvable')
        return dict(id=ident, run_id=row[0], started_at=row[1], ends_at=row[2], **json.loads(row[3]),
                    points=[dict(at=p[0], positions=json.loads(p[1])) for p in points])

    def update(self, ident, source):
        watch = self.get(ident)
        if watch['origin'] == 'SYNTHETIC_DEMO':
            return watch
        now = self.store.clock()
        if watch['points'] and now-watch['points'][-1]['at'] < 15:
            return watch
        rows = []
        for p in watch['positions']:
            try:
                raw = source._request('GET', '/market/'+quote(p['market_id'], safe=''))
                if raw.get('id') != p['market_id'] or raw.get('token') != 'MANA':
                    raise InvalidData('Identité/token incohérent')
                if raw.get('outcomeType') == 'BINARY':
                    proba = number(raw.get('probability'), 'probability', 0, 1)
                    resolution = raw.get('resolution') if raw.get('isResolved') is True else None
                else:
                    answer = next(a for a in raw.get('answers', []) if a.get('id') == p['answer_id'])
                    proba = number(answer.get('prob', answer.get('probability')), 'probability', 0, 1)
                    resolution = answer.get('resolution')
                probability = proba if p['side'] == 'YES' else 1-proba
                settled = resolution in ('YES', 'NO')
                if settled:
                    probability = float(resolution == p['side'])
                value = p['shares']*probability
                state = 'PAPER_SETTLED' if settled else ('CLOSED_AWAITING_RESOLUTION' if raw.get('closeTime', float('inf')) <= now*1000 else 'PAPER_MARK_TO_MARKET')
                if resolution is not None and resolution not in ('YES', 'NO') or raw.get('isResolved') is True and not settled:
                    state, value = 'RESOLVED_UNSUPPORTED_PAYOUT', None
                rows.append(dict(market_id=p['market_id'], question=p['question'], probability=probability,
                    value=value, pnl_marked=None if value is None else value-p['estimated_debit'],
                    state=state, settled=settled))
            except Exception as exc:
                rows.append(dict(market_id=p['market_id'], question=p['question'], value=None, pnl_marked=None,
                                 state='DATA_UNAVAILABLE', error_type=type(exc).__name__, settled=False))
        with self.store.connect() as db:
            db.execute('INSERT INTO observation_points VALUES(?,?,?)', (ident, now, json.dumps(rows)))
        return self.get(ident)

    def start_probe(self, report, source, market_id, answer_id, side, minutes, *, confirmed=False):
        """Explicit 1-Mana PAPER measurement when no strategy position qualifies.
        Its expected return may be negative; never relabel it an optimizer win.
        """
        if confirmed is not True or report.get('data_origin') != 'MANIFOLD_AND_OPENAI_PREVIEW':
            raise InvalidData('Confirmer une expérience papier sur données réelles')
        analysis = next((a for a in report['analyses'] if a['market_id']==market_id), None)
        old = next((m for m in report['markets'] if m['id']==market_id), None)
        if not old or not analysis or side not in ('YES','NO'):
            raise InvalidData('Dossier analysé et sens valides requis')
        fresh = source.fetch(market_id, old['domain'], old['family'])
        if fresh.conditions_hash != analysis['conditions_hash']:
            raise InvalidData('Les critères ont changé')
        cutoff = report.get('horizon_cutoff')
        if cutoff is not None and fresh.close_time > cutoff:
            raise InvalidData('Marché hors horizon demandé')
        if not 0 <= self.store.clock()-analysis['completed_at'] <= 300:
            raise InvalidData('Analyse périmée')
        answer = next((a for a in fresh.answers if a.id==answer_id),None)
        if answer is None:
            raise InvalidData('Réponse introuvable')
        mode = ROUND_CEILING if side=='YES' else ROUND_FLOOR
        limit = float(Decimal(str(answer.probability+(.02 if side=='YES' else -.02))).quantize(Decimal('.01'),rounding=mode))
        if not .01 <= limit <= .99:
            raise InvalidData('Prix trop extrême pour prévisualiser')
        q = source.preview(fresh, answer_id, side, 1.0, limit)
        if not q.full_fill or q.shares<=0 or (q.market_id,q.answer_id,q.side)!=(market_id,answer_id,side):
            raise InvalidData('Aucun remplissage complet prévisualisé pour cette expérience')
        p = number(analysis['final'][answer_id],'forecast',0,1)
        price = q.probability_before
        if side=='NO':
            p,price = 1-p,1-price
        debit = number(q.estimated_debit,'estimated_debit',1)
        position = dict(market_id=market_id, question=fresh.question, answer_id=answer_id,
            answer_label=answer.label, side=side, domain=fresh.domain, family=fresh.family,
            amount=1.0, estimated_debit=debit, shares=q.shares, agent_probability=p,
            market_probability=price, quoted_at=q.quoted_at, expected_profit=p*q.shares-debit,
            quote_source=q.source)
        probe = dict(run_id=report['run_id']+':probe:'+answer_id+':'+side,
            data_origin=report['data_origin'], observation_label='EXPERIENCE_PAPIER_1_MANA_HORS_STRATEGIE',
            plan=dict(selected=[position],expected_profit=position['expected_profit']))
        return self.start(probe,minutes)
