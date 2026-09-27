"""Offline regression tests: no provider credentials, network or Mana spent."""
from dataclasses import replace
import itertools
import json
import math
import random
import socket
import time
from types import SimpleNamespace

import pytest

from portfolio.demo import DemoResearcher, DemoSource, build_demo, markets
from portfolio.engine import allocate, run_cycle
from portfolio.manifold import Manifold, normalize, TOPICS, RemoteError
from portfolio.models import Analysis, Answer, Holdings, InvalidData, Option, Policy
from portfolio.research import BudgetExceeded, ForecastOutput, Researcher, ResearchPolicy, Store

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **kw):
        raise AssertionError('Network access forbidden in tests')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    for name in ('OPENAI_API_KEY', 'MANIFOLD_API_KEY', 'OPENAI_MODEL', 'PORTFOLIO_MODEL',
                 'PORTFOLIO_INPUT_USD_PER_M', 'PORTFOLIO_OUTPUT_USD_PER_M'):
        monkeypatch.delenv(name, raising=False)


def raw(mid='m', multi=False, independent=False):
    data = dict(id=mid, question='Will the spacecraft complete its test?',
                textDescription='Resolves YES only after the published test report confirms success.',
                token='MANA', isResolved=False, closeTime=(NOW+86400)*1000,
                outcomeType='BINARY', mechanism='cpmm-1', probability=.4,
                volume24Hours=500, groupSlugs=['space'])
    if multi:
        data.update(outcomeType='MULTIPLE_CHOICE', mechanism='cpmm-multi-1',
                    shouldAnswersSumToOne=not independent, answers=[
                        dict(id='a', text='A', prob=.3), dict(id='b', text='B', prob=.7)])
    return data


def test_binary_and_units():
    market = normalize(raw(), 'Space', NOW)
    assert market.kind == 'BINARY' and market.answers[0].probability == .4
    assert market.close_time == NOW + 86400
    assert market.description != market.question


@pytest.mark.parametrize('value', [0.0, 1.0])
def test_extreme_valid_probabilities_not_replaced(value):
    data = raw(); data['probability'] = value
    assert normalize(data, 'Space', NOW).answers[0].probability == value


@pytest.mark.parametrize('change', [
    {'token': None}, {'token': 'CASH'}, {'isResolved': True}, {'isResolved': None},
    {'isClosed': True}, {'closeTime': NOW*1000}, {'closeTime': None},
    {'probability': float('nan')}, {'probability': True}, {'probability': 1.1},
    {'mechanism': 'dpm-2'}, {'outcomeType': 'POLL'},
    {'textDescription': None, 'description': None},
    {'question': 'Will the next election have more ballots?'},
    {'groupSlugs': ['politics']},
])
def test_bad_markets_fail_closed(change):
    data = raw(); data.update(change)
    with pytest.raises(InvalidData):
        normalize(data, 'Space', NOW)


def test_multi_correct_ids_and_native_prob():
    m = normalize(raw(multi=True), 'Science', NOW)
    assert m.kind == 'SUM_TO_ONE' and [a.id for a in m.answers] == ['a', 'b']


def test_independent_probabilities_do_not_have_to_sum_to_one():
    r = raw(multi=True, independent=True); r['answers'][0]['prob'] = .8
    assert sum(a.probability for a in normalize(r, 'AI', NOW).answers) == 1.5


@pytest.mark.parametrize('problem', ['sum', 'duplicate', 'missing_type', 'too_many', 'partial'])
def test_bad_multi_is_not_silently_normalized(problem):
    r = raw(multi=True)
    if problem == 'sum': r['answers'][0]['prob'] = .8
    if problem == 'duplicate': r['answers'][1]['id'] = 'a'
    if problem == 'missing_type': r.pop('shouldAnswersSumToOne')
    if problem == 'too_many': r['answers'] *= 5
    if problem == 'partial': r['answers'][0]['resolution'] = 'YES'
    with pytest.raises(InvalidData): normalize(r, 'AI', NOW)


def test_contract_hash_changes_for_terms_not_prices():
    r = raw(); first = normalize(r, 'AI', NOW)
    r['probability'] = .6
    assert normalize(r, 'AI', NOW).conditions_hash == first.conditions_hash
    r['textDescription'] += ' Additional resolution rule.'
    assert normalize(r, 'AI', NOW).conditions_hash != first.conditions_hash


class Session:
    def __init__(self, result): self.result, self.calls = result, []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.result(method, url, kwargs) if callable(self.result) else self.result
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)


def bet(**kw):
    return dict(betId='dry-run', contractId='m', answerId='a', outcome='NO',
                amount=5, shares=9, probBefore=.3, isFilled=True,
                fees={'creatorFee': .1}, **kw)


def test_multichoice_preview_always_dry_run_and_answer_id():
    session = Session(bet())
    client = Manifold(session=session, api_key='TEST_NOT_A_SECRET', clock=lambda: NOW)
    q = client.preview(normalize(raw(multi=True), 'AI', NOW), 'a', 'NO', 5, .6)
    method, url, kw = session.calls[0]
    assert method == 'POST' and kw['json']['dryRun'] is True
    assert kw['json']['answerId'] == 'a' and kw['json']['outcome'] == 'NO'
    assert 'Authorization' in kw['headers'] and 'Authorization' not in kw['json']
    assert q.estimated_debit == pytest.approx(6.1)
    assert q.full_fill


def test_binary_preview_omits_answer_id():
    b = bet(); b.update(outcome='YES', probBefore=.4)
    session = Session(b)
    Manifold(session=session, api_key='test', clock=lambda: NOW).preview(
        normalize(raw(), 'Space', NOW), 'YES', 'YES', 5, .5)
    assert 'answerId' not in session.calls[0][2]['json']


@pytest.mark.parametrize('change', [{'betId': 'a-real-id'}, {'contractId': 'other'}, {'answerId': 'other'}, {'shares': float('nan')}])
def test_invalid_preview_is_rejected(change):
    b = bet(); b.update(change)
    client = Manifold(session=Session(b), api_key='test', clock=lambda: NOW)
    with pytest.raises(InvalidData): client.preview(normalize(raw(multi=True), 'AI', NOW), 'a', 'NO', 5, .6)


def test_missing_key_fails_before_post():
    s = Session(bet()); c = Manifold(session=s, api_key='', clock=lambda: NOW)
    with pytest.raises(RemoteError): c.preview(normalize(raw(), 'AI', NOW), 'YES', 'YES', 5, .5)
    assert not s.calls


def test_request_budget_is_hard():
    c = Manifold(session=Session(raw()), max_requests=1, clock=lambda: NOW)
    c.fetch('m', 'Space')
    with pytest.raises(RemoteError): c.fetch('m', 'Space')


def test_discovery_round_robin_domains_and_types():
    values = {}
    def respond(method, url, kw):
        if url.endswith('search-markets'):
            p = kw['params']; mid = p['topicSlug'] + p['contractType']
            r = raw(mid, multi=p['contractType'] == 'MULTIPLE_CHOICE')
            values[mid] = r
            return [r]
        return values[url.rsplit('/', 1)[-1]]
    c = Manifold(session=Session(respond), clock=lambda: NOW)
    found, _ = c.discover(['AI', 'Space'], limit=4)
    assert len(found) == 4
    assert {m.kind for m in found} == {'BINARY', 'SUM_TO_ONE'}
    assert {m.domain for m in found} == {'AI', 'Space'}


def test_discovery_deduplicates_topics():
    s = Session(lambda method, url, kw: [raw()] if url.endswith('search-markets') else raw())
    found, _ = Manifold(session=s, clock=lambda: NOW).discover(['AI', 'Science'], limit=8)
    assert len(found) == 1
    assert sum('/market/' in url for _, url, _ in s.calls) == 1


def output(p=.6, source='https://example.org/report'):
    return ForecastOutput(probabilities=[{'answer_id': 'YES', 'probability': p}],
        confidence='high', reasoning='Brief, public, test explanation.', evidence=[
            {'title': 'Test report', 'url': source, 'summary': 'Synthetic evidence for unit tests.'}])


class FakeClient:
    def __init__(self, responses=None):
        self.calls, self.options = [], []
        self.queue = list(responses or [output(.63), output(.6)])
        self.responses = self
    def with_options(self, **kw): self.options.append(kw); return self
    def parse(self, **kw):
        self.calls.append(kw)
        out = self.queue.pop(0)
        if isinstance(out, Exception): raise out
        data = dict(status='completed', usage={'input_tokens': 100, 'output_tokens': 50},
                    output=[{'type': 'web_search_call', 'status': 'completed',
                             'action': {'sources': [{'url': 'https://example.org/report'}]}}])
        return SimpleNamespace(output_parsed=out, model_dump=lambda **_: data)


def researcher(tmp_path, client=None, policy=None, clock=lambda: NOW):
    s = Store(tmp_path/'state.sqlite3', clock=clock)
    return s, Researcher(s, model='gpt-6-luna', client=client or FakeClient(), policy=policy or ResearchPolicy())


def test_researcher_critic_structured_trace_and_blind_prompt(tmp_path):
    client = FakeClient(); s, r = researcher(tmp_path, client)
    a = r.analyse(normalize(raw(), 'AI', NOW), 'run')
    assert a.initial['YES'] == .63 and a.final['YES'] == .6 and a.critic_completed
    assert {e.source_agent for e in a.evidence} == {'researcher', 'critic'}
    context = json.loads(client.calls[0]['input'][1]['content'])
    assert 'probability' not in context['answers'][0]
    assert all(c['max_tool_calls'] == 1 and c['max_output_tokens'] == 2400 for c in client.calls)
    assert all(c['max_retries'] == 0 for c in client.options)
    assert s.usage('run')['calls'] == 2 and s.usage('run')['known_cost_usd'] > 0


def test_cache_avoids_repeated_cost_and_keeps_timestamp(tmp_path):
    client = FakeClient(); s, r = researcher(tmp_path, client)
    m = normalize(raw(), 'AI', NOW)
    first = r.analyse(m, 'one'); second = r.analyse(m, 'two')
    assert len(client.calls) == 2 and s.usage('two')['calls'] == 0
    assert first.completed_at == second.completed_at and second.source == 'openai-cache'


def test_cache_expiry(tmp_path):
    clock = [NOW]; client = FakeClient([output(), output(), output(), output()])
    _, r = researcher(tmp_path, client, clock=lambda: clock[0])
    m = normalize(raw(), 'AI', NOW)
    r.analyse(m, 'one'); clock[0] += 301; r.analyse(m, 'two')
    assert len(client.calls) == 4


def test_invented_sources_do_not_become_evidence(tmp_path):
    client = FakeClient([output(source='https://example.org/invented')]); _, r = researcher(tmp_path, client)
    a = r.analyse(normalize(raw(), 'AI', NOW), 'run')
    assert not a.evidence and not a.critic_completed and len(client.calls) == 1


def test_critic_failure_not_hidden_and_stays_budgeted(tmp_path):
    client = FakeClient([output(), RuntimeError('offline')]); s, r = researcher(tmp_path, client)
    a = r.analyse(normalize(raw(), 'AI', NOW), 'run')
    assert not a.critic_completed and a.confidence == 'medium'
    assert s.usage('run')['unknown_cost_calls'] == 1
    assert s.usage('run')['charged_or_reserved_usd'] >= .25


def test_deadline_stops_before_api_call(tmp_path):
    client = FakeClient(); s, r = researcher(tmp_path, client)
    with pytest.raises(BudgetExceeded):
        r.analyse(normalize(raw(), 'AI', NOW), 'run', deadline=time.monotonic()-1)
    assert not client.calls and s.usage('run')['calls'] == 0


def test_budget_persists_across_instances_and_failed_requests(tmp_path):
    p = ResearchPolicy(calls_per_day=1)
    s = Store(tmp_path/'b.sqlite3', clock=lambda: NOW)
    ident = s.reserve('one', 'researcher', 'unknown-model', p)
    s.finish(ident, None, 'unknown-model', 'FAILED')
    reopened = Store(tmp_path/'b.sqlite3', clock=lambda: NOW)
    with pytest.raises(BudgetExceeded): reopened.reserve('two', 'researcher', 'unknown-model', p)
    assert reopened.usage('one')['known_cost_usd'] == 0
    assert reopened.usage('one')['unknown_cost_calls'] == 1


def test_unknown_price_never_reported_as_free(tmp_path):
    s, r = researcher(tmp_path); r.model = 'model-not-in-rate-table'
    r.analyse(normalize(raw(), 'AI', NOW), 'run')
    assert s.usage('run')['unknown_cost_calls'] == 2
    assert s.usage('run')['charged_or_reserved_usd'] == .5


def option(mid, cost, profit, domain='AI', family=None, suffix=''):
    return Option(mid+str(cost)+suffix, mid, mid, 'YES', 'YES', 'YES', domain, family or domain,
                  cost, cost, (cost+profit)/.75, .75, .4, profit, 'fixture', NOW)


def test_global_not_greedy():
    opts = [option('a', 6, 8), option('b', 5, 7), option('c', 5, 7)]
    p = Policy(bankroll=10, cash_reserve=0, max_total=10)
    plan = allocate(opts, p)
    assert {o.market_id for o in plan.selected} == {'b', 'c'}
    assert plan.expected_profit == 14 and plan.estimated_debit == 10


def test_domain_and_family_caps():
    opts = [option('a', 10, 8, 'AI', 'common'), option('b', 10, 7, 'Space', 'common'), option('c', 10, 6, 'Science')]
    plan = allocate(opts, Policy(max_family=10))
    assert {o.market_id for o in plan.selected} == {'a', 'c'}
    plan = allocate([option('a', 10, 8), option('b', 10, 7)], Policy(max_domain=10))
    assert len(plan.selected) == 1


def test_existing_positions_and_total_room():
    holdings = Holdings(by_market={'a': 10}, by_domain={'AI': 10}, by_family={'AI': 10}, total=10)
    opts = [option('a', 5, 20), option('b', 10, 7), option('c', 10, 6)]
    plan = allocate(opts, Policy(max_total=20), holdings)
    assert {o.market_id for o in plan.selected} == {'b'}


def test_stay_cash_if_nonpositive():
    plan = allocate([option('a', 10, -1), option('b', 10, 0)], Policy())
    assert not plan.selected and plan.expected_profit == 0 and plan.cash_remaining == 100


def test_one_leg_per_parent_market():
    opts = [option('a', 5, 2, suffix='answer1'), option('a', 10, 3, suffix='answer2')]
    assert len(allocate(opts, Policy()).selected) == 1


def test_duplicate_options_rejected():
    o = option('a', 5, 2)
    with pytest.raises(InvalidData): allocate([o, o], Policy())


def test_fabricated_gain_rejected():
    o = replace(option('a', 5, 2), expected_profit=999)
    with pytest.raises(InvalidData): allocate([o], Policy())


def test_bounded_search_reports_limit():
    plan = allocate([option('a', 5, 1), option('b', 5, 2)], Policy(max_nodes=2))
    assert not plan.optimal_for_supplied_options and plan.estimated_debit <= 60


def test_random_small_knapsacks_match_brute_force():
    rng = random.Random(7)
    p = Policy(bankroll=20, cash_reserve=0, max_total=20, max_domain=15, max_family=15)
    for _ in range(25):
        groups = [[option(str(i), c, rng.randint(0, 12), domain=('AI' if i % 2 else 'Space')) for c in (5, 10)] for i in range(4)]
        best = 0
        for selection in itertools.product(*[[None]+g for g in groups]):
            chosen = [o for o in selection if o]
            if sum(o.estimated_debit for o in chosen) > 20: continue
            if any(sum(o.estimated_debit for o in chosen if o.domain == d) > 15 for d in ('AI','Space')): continue
            best = max(best, sum(o.expected_profit for o in chosen))
        plan = allocate([o for g in groups for o in g], p)
        assert plan.expected_profit == pytest.approx(best)
        assert plan.optimal_for_supplied_options


def test_demo_full_pipeline_no_network_no_claimed_pnl():
    r = build_demo()
    assert r['mode'] == 'PREVIEW_ONLY' and r['data_origin'] == 'SYNTHETIC_DEMO'
    assert {m['kind'] for m in r['markets']} == {'BINARY', 'SUM_TO_ONE', 'INDEPENDENT'}
    assert r['plan']['selected'] and r['plan']['estimated_debit'] <= 60
    assert r['realized_pnl_mana'] is None and r['usage']['calls'] == 0
    json.dumps(r, allow_nan=False)


def test_demo_domain_selection():
    r = build_demo(domains=['Space'])
    assert {m['domain'] for m in r['markets']} == {'Space'}


def test_changed_contract_refuses_preview():
    ms = markets(lambda: NOW)
    class Changed(DemoSource):
        def fetch(self, *args): return replace(super().fetch(*args), description='Changed rules')
    r = run_cycle(ms, DemoResearcher(clock=lambda: NOW), Changed(ms, clock=lambda: NOW), clock=lambda: NOW)
    assert not r['plan']['selected']
    assert any('Critères' in e['reason'] for e in r['errors'])


def test_partial_quotes_do_not_enter_optimizer():
    ms = markets(lambda: NOW)
    class Partial(DemoSource):
        def preview(self, *args): return replace(super().preview(*args), full_fill=False)
    r = run_cycle(ms, DemoResearcher(clock=lambda: NOW), Partial(ms, clock=lambda: NOW), clock=lambda: NOW)
    assert not r['plan']['selected']


def test_stale_quotes_rejected():
    ms = markets(lambda: NOW)
    class Stale(DemoSource):
        def preview(self, *args): return replace(super().preview(*args), quoted_at=NOW-60)
    r = run_cycle(ms, DemoResearcher(clock=lambda: NOW), Stale(ms, clock=lambda: NOW), clock=lambda: NOW)
    assert not r['plan']['selected']


def test_report_and_cache_serialization(tmp_path):
    store = Store(tmp_path/'s.sqlite3')
    r = build_demo(); store.save_report(r['run_id'], r)
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT payload FROM reports').fetchone()[0])
    assert saved['plan'] == r['plan'] or saved['plan']['expected_profit'] == r['plan']['expected_profit']


def test_end_to_end_real_adapters_with_fake_transports(tmp_path):
    def respond(method, url, kw):
        if method == 'GET': return raw()
        payload = kw['json']
        assert payload['dryRun'] is True
        return dict(betId='dry-run', contractId='m', outcome=payload['outcome'],
                    amount=payload['amount'], shares=payload['amount']/.4,
                    probBefore=.4, isFilled=True, fees={})
    session = Session(respond)
    source = Manifold(session=session, api_key='fixture', clock=lambda: NOW)
    s, r = researcher(tmp_path)
    report = run_cycle([normalize(raw(), 'AI', NOW)], r, source, store=s, clock=lambda: NOW)
    assert len(report['plan']['selected']) == 1
    assert report['usage']['calls'] == 2
    assert report['usage']['unknown_cost_calls'] == 0
    assert report['plan']['estimated_debit'] <= 20
    assert all(kw['json']['dryRun'] for method, _, kw in session.calls if method == 'POST')


def test_multi_forecast_requires_exact_answer_ids():
    m = normalize(raw(multi=True), 'AI', NOW)
    a = Analysis(m.id, m.conditions_hash, {'a': .3, 'b': .7}, {'a': .4, 'typo': .6},
                 'high', (), 'test', False, NOW)
    with pytest.raises(InvalidData): a.validate(m)
