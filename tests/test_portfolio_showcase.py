"""Regression tests for workflow, horizon filtering and PAPER observations.
No network, no API credentials, no live bets and no paid calls.
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import socket
import time

import pytest

from portfolio.demo import DemoSource, DemoResearcher, markets, build_demo
from portfolio.models import Policy, InvalidData
from portfolio.research import Store, ForecastOutput
from portfolio.showcase import HorizonManifold, NarratedResearcher, run_showcase, error_info
from portfolio.observation import Observation

NOW = 1800000000.0


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*a,**kw):
        raise AssertionError('Network forbidden')
    monkeypatch.setattr(socket.socket,'connect',blocked)
    monkeypatch.setattr(socket,'create_connection',blocked)
    for key in ('OPENAI_API_KEY','MANIFOLD_API_KEY','PORTFOLIO_MODEL','OPENAI_MODEL'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr('dotenv.load_dotenv',lambda *a,**kw:None)


def raw(mid='m',closes=NOW+300):
    return dict(id=mid,question='Will the spacecraft complete its test?',
        textDescription='Resolves YES only after the official report confirms success.',token='MANA',
        isResolved=False,closeTime=closes*1000,outcomeType='BINARY',mechanism='cpmm-1',
        probability=.4,volume24Hours=500,groupSlugs=['space'])


class Session:
    def __init__(self,reply):
        self.reply,self.calls=reply,[]
    def request(self,method,url,**kw):
        self.calls.append((method,url,kw))
        result=self.reply(method,url,kw)
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:result)


def test_fifteen_minute_filter_uses_close_time_not_resolution():
    values={'near':raw('near',NOW+600),'far':raw('far',NOW+3600)}
    def reply(method,url,kw):
        if url.endswith('search-markets'):
            assert kw['params']['sort']=='close-date'
            return list(values.values())
        return values[url.rsplit('/',1)[-1]]
    source=HorizonManifold(horizon_minutes=15,session=Session(reply),clock=lambda:NOW)
    found,errors=source.discover(['Space'],limit=3)
    assert [m.id for m in found]==['near']
    assert any(e.get('code')=='OUTSIDE_HORIZON' for e in errors)
    assert source.cutoff==NOW+900


def test_empty_horizon_is_explicit_not_a_model_failure():
    source=HorizonManifold(horizon_minutes=15,session=Session(lambda *a:[]),clock=lambda:NOW)
    found,errors=source.discover(['AI'],limit=2)
    assert not found and any(e.get('code')=='NO_MARKET_WITHIN_HORIZON' for e in errors)


def test_horizon_is_not_silently_extended_when_fetching_later():
    clock=[NOW]
    source=HorizonManifold(horizon_minutes=15,session=Session(lambda *a:raw(closes=NOW+901)),clock=lambda:clock[0])
    clock[0]+=30
    with pytest.raises(InvalidData,match='horizon'):
        source.fetch('m','Space')


def test_completed_small_quote_survives_larger_tier_failure():
    values=markets(lambda:NOW)[:1]
    class PartialFailure(DemoSource):
        def preview(self,*args):
            if args[3]>5:
                raise RuntimeError('TEST_API_ERROR')
            return super().preview(*args)
    source=PartialFailure(values,clock=lambda:NOW)
    report=run_showcase(values,DemoResearcher(clock=lambda:NOW),source,clock=lambda:NOW)
    assert len(report['plan']['selected'])==1
    assert report['plan']['selected'][0]['amount']==5
    assert any(d.get('code')=='RuntimeError' for d in report['diagnostics'])
    assert report['preview_seconds_reserved']>0
    assert report['research_seconds_reserved']<180


def test_zero_edge_is_never_forced_into_strategy():
    values=markets(lambda:NOW)[-1:]
    report=run_showcase(values,DemoResearcher(clock=lambda:NOW),DemoSource(values,clock=lambda:NOW),clock=lambda:NOW)
    assert not report['plan']['selected']
    assert any(d.get('edge_points')==0 for d in report['diagnostics'])
    assert report['realized_pnl_mana'] is None


def test_analysis_failure_is_distinct_from_zero_edge():
    class Failed:
        def analyse(self,*a,**kw):
            raise InvalidData('Pas de critères vérifiables')
    values=markets(lambda:NOW)[:1]
    report=run_showcase(values,Failed(),DemoSource(values,clock=lambda:NOW),clock=lambda:NOW)
    assert report['analyses']==[]
    assert report['errors'][0]['reason']=='Pas de critères vérifiables'
    assert report['errors'][0]['stage']=='analyse'


def test_error_messages_do_not_leak_request_secrets():
    class HTTPFailure(Exception):
        status_code=401
    value=error_info(HTTPFailure('secret-key-should-not-appear'))
    assert 'secret-key' not in str(value)
    assert value['http_status']==401 and 'Clé' in value['reason']


class Client:
    def __init__(self):
        self.responses=self
        self.count=0
    def with_options(self,**kw):
        return self
    def parse(self,**kw):
        self.count+=1
        out=ForecastOutput(probabilities=[dict(answer_id='YES',probability=.65 if self.count==1 else .60)],
            confidence='high',reasoning='Explication researcher' if self.count==1 else 'Explication critic',
            evidence=[dict(title='Test report',url='https://example.org/report',summary='Fixture evidence')])
        data=dict(status='completed',usage=dict(input_tokens=100,output_tokens=80),output=[
            dict(type='web_search_call',status='completed',action=dict(sources=[dict(url='https://example.org/report')]))])
        return SimpleNamespace(output_parsed=out,model_dump=lambda **k:data)


def test_both_public_explanations_and_phases_are_retained(tmp_path):
    values=markets(lambda:NOW)[:1]
    store=Store(tmp_path/'s.db',clock=lambda:NOW)
    researcher=NarratedResearcher(store,model='gpt-6-luna',client=Client())
    report=run_showcase(values,researcher,DemoSource(values,clock=lambda:NOW),store=store,clock=lambda:NOW)
    phases=report['phase_reports'][values[0].id]
    assert phases['researcher']['reasoning']=='Explication researcher'
    assert phases['critic']['reasoning']=='Explication critic'
    assert any(e['stage']=='researcher' and e['status']=='DONE' for e in report['events'])
    assert any(e['stage']=='critic' and e['status']=='DONE' for e in report['events'])
    assert report['analyses'][0]['initial']['YES']==.65
    assert report['analyses'][0]['final']['YES']==.60


def paper_report():
    return dict(run_id='paper-test',data_origin='MANIFOLD_AND_OPENAI_PREVIEW',
        plan=dict(expected_profit=1,selected=[dict(market_id='m',question='Test',answer_id='YES',side='YES',
            shares=10,market_probability=.4,estimated_debit=5,quoted_at=NOW)]))


class ReadsOnly:
    def __init__(self,data):
        self.data,self.calls=data,[]
    def _request(self,method,path,**kw):
        assert method=='GET'
        self.calls.append((method,path))
        return self.data


def test_paper_open_is_persistent_and_idempotent(tmp_path):
    store=Store(tmp_path/'s.db',clock=lambda:NOW)
    obs=Observation(store)
    first=obs.start(paper_report(),15)
    assert obs.start(paper_report(),15)==first
    assert obs.get(first)['ends_at']==NOW+900
    assert len(obs.get(first)['points'])==1


def test_close_does_not_mean_settled_or_paid(tmp_path):
    clock=[NOW]
    obs=Observation(Store(tmp_path/'s.db',clock=lambda:clock[0]))
    ident=obs.start(paper_report(),15)
    clock[0]+=901
    value=raw(closes=NOW+900)
    value['probability']=.5
    watch=obs.update(ident,ReadsOnly(value))
    p=watch['points'][-1]['positions'][0]
    assert p['state']=='CLOSED_AWAITING_RESOLUTION'
    assert p['settled'] is False
    assert p['pnl_marked']==0


def test_binary_paper_settlement_uses_confirmed_resolution(tmp_path):
    clock=[NOW]
    obs=Observation(Store(tmp_path/'s.db',clock=lambda:clock[0]))
    ident=obs.start(paper_report(),15)
    clock[0]+=20
    value=raw(); value.update(isResolved=True,resolution='NO')
    p=obs.update(ident,ReadsOnly(value))['points'][-1]['positions'][0]
    assert p['settled'] is True and p['pnl_marked']==-5
    assert p['state']=='PAPER_SETTLED'


def test_failed_price_is_unknown_not_zero_pnl(tmp_path):
    clock=[NOW]
    obs=Observation(Store(tmp_path/'s.db',clock=lambda:clock[0]))
    ident=obs.start(paper_report(),15); clock[0]+=20
    p=obs.update(ident,ReadsOnly({}))['points'][-1]['positions'][0]
    assert p['pnl_marked'] is None and p['state']=='DATA_UNAVAILABLE'


def test_synthetic_observation_never_invents_new_prices(tmp_path):
    clock=[NOW]
    obs=Observation(Store(tmp_path/'s.db',clock=lambda:clock[0]))
    report=paper_report();report['data_origin']='SYNTHETIC_DEMO'
    ident=obs.start(report,15);clock[0]+=60
    source=ReadsOnly(raw())
    watch=obs.update(ident,source)
    assert len(watch['points'])==1 and not source.calls


def test_stale_quote_cannot_start_paper_experiment(tmp_path):
    obs=Observation(Store(tmp_path/'s.db',clock=lambda:NOW+121))
    with pytest.raises(InvalidData,match='anciennes'):
        obs.start(paper_report(),15)


def test_ui_shows_reasoning_workflow_horizon_and_paper_label(tmp_path,monkeypatch):
    pytest.importorskip('streamlit')
    from streamlit.testing.v1 import AppTest
    original=Store
    monkeypatch.setattr('portfolio.research.Store',lambda:original(tmp_path/'ui.db'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'portfolio_app.py')).run(timeout=20)
    app.selectbox[0].set_value('15 minutes')
    app.button[0].click().run(timeout=20)
    assert not app.exception
    assert any('Synthèse finale' in e.value for e in app.subheader)
    assert any('Researcher' in e.value for e in app.subheader)
    assert any('Critic' in e.value for e in app.subheader)
    assert any('Trace complète' in e.value for e in app.subheader)
    assert any('paiement' in e.value for e in app.info)
    buttons=[b for b in app.button if b.key=='observe_plan']
    assert buttons
    buttons[0].click().run(timeout=20)
    assert not app.exception
    assert any('Positions PAPIER' in e.label for e in app.metric)
