"""Tests hors ligne de D et exemples exécutables du contrat A/B/D.

Aucune clé, aucun accès réseau, aucune mise réelle. Les faux clients ci-dessous
simulent des réponses contrôlées ; ils ne prouvent pas le fonctionnement de
l'adaptateur Manifold de B. Lancer depuis la racine :
    python -m unittest discover -s tests -p 'test_d.py' -v
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent.schemas import AgentAnalysis, Evidence, Forecast
from tools.budget import BudgetEngine, BudgetExceeded, BudgetPolicy
from tools.cache import Cache
from tools.integration import (
    ExecutionReport, GateService, Pipeline, Quote, consolidate_portfolio,
)
from tools.risk import (
    AnalysisEnvelope, MarketSnapshot, PortfolioOrder, PortfolioSnapshot,
    RiskPolicy, conservative_limit,
)
from tools.tracker import StateConflict, Tracker, mana, units

URL = "https://example.org/official"


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1):
        self.now += seconds


class FakeResponse:
    def __init__(self, p, urls, *, status="completed", parsed=True):
        self.p, self.urls, self.status = p, urls, status
        self.output_parsed = Forecast(probability=p, confidence="high", reasoning="Test hors ligne") if parsed else None

    def model_dump(self, **kwargs):
        return {"id": "fake-response", "status": self.status,
                "output": [{"type": "web_search_call", "status": "completed", "action": {
                    "type": "search", "sources": [{"url": u} for u in self.urls]}}],
                "usage": {"input_tokens": 50, "output_tokens": 20}}


class FakeClient:
    def __init__(self, *, probabilities=(0.64, 0.60), urls=(URL,), error=None):
        self.probabilities = list(probabilities)
        self.urls, self.error = urls, error
        self.calls, self.options = [], []
        self.responses = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        p = self.probabilities[min(len(self.calls)-1, len(self.probabilities)-1)]
        response = FakeResponse(p, self.urls)
        if not kwargs.get("tools"):
            response.model_dump = lambda **kw: {"id": "no-web", "status": "completed", "output": [], "usage": {}}
        return response


class FakeBroker:
    account_id = "test-account"

    def __init__(self, clock):
        self.clock = clock
        self.markets = {}
        self.cash = 100.0
        self.debited_today = 0.0
        self.loss_today = 0.0
        self.remote_orders = {}
        self.reports = {}
        self.place_calls = 0
        self.preview_calls = 0
        self.mode = "filled"
        self.no_lookup = False
        self.lagging_portfolio = False
        self.market_age = 0
        self.portfolio_age = 0
        self.quote_change = None
        self.preview_barrier = None
        self.lock = threading.RLock()

    def add_market(self, market_id="m1", **changes):
        market = MarketSnapshot(market_id, "Will product X launch?", "YES only if a public release occurs before close.",
                                0.42, "MANA", "BINARY", "cpmm-1", False, False, self.clock()+3600, self.clock())
        self.markets[market_id] = replace(market, **changes)

    def fetch_market(self, market_id):
        with self.lock:
            return replace(self.markets[market_id], fetched_at=self.clock()-self.market_age)

    def fetch_portfolio(self):
        with self.lock:
            return PortfolioSnapshot(self.account_id, 100.0 if self.lagging_portfolio else self.cash,
                                     self.clock()-self.portfolio_age,
                                     () if self.lagging_portfolio else tuple(self.remote_orders.values()),
                                     0 if self.lagging_portfolio else self.debited_today, self.loss_today)

    def preview_order(self, *, market_id, outcome, max_total_debit, limit_probability, order_expires_at):
        with self.lock:
            self.preview_calls += 1
        if self.preview_barrier:
            self.preview_barrier.wait(timeout=5)
        # Valorisation fictive : prix constant + 1 Mana de frais, uniquement
        # pour les tests. Ce n'est PAS la tarification réelle de Manifold.
        price = self.markets[market_id].probability
        outcome_price = price if outcome == "YES" else 1-price
        quote = Quote(market_id, outcome, max_total_debit-1, max_total_debit,
                      limit_probability, order_expires_at, self.clock(), True, True,
                      estimated_debit=max_total_debit,
                      payout_if_correct=(max_total_debit-1)/outcome_price,
                      payout_if_incorrect=0.0, full_fill_estimated=True,
                      reference_probability=price)
        return self.quote_change(quote) if self.quote_change else quote

    def _report(self, auth, state, spent, remaining, external=True):
        ext = "bet-"+auth.authorization_id if external else None
        return ExecutionReport(state, spent, remaining, ext, self.clock(), "fake-confirmation")

    def install_report(self, auth, report):
        with self.lock:
            previous = self.remote_orders.get(report.external_id)
            before = previous.debit if previous else 0
            self.cash -= report.cumulative_debit-before
            self.debited_today += report.cumulative_debit-before
            if report.external_id:
                self.remote_orders[report.external_id] = PortfolioOrder(
                    report.external_id, auth.market_id, auth.outcome,
                    report.cumulative_debit, report.remaining_debit, True)
            self.reports[auth.authorization_id] = report

    def place_order(self, auth):
        with self.lock:
            self.place_calls += 1
        if self.mode == "timeout_no_fill":
            raise TimeoutError("test")
        if self.mode == "reject":
            return self._report(auth, "REJECTED", 0, 0, external=False)
        if self.mode == "partial":
            report = self._report(auth, "PARTIAL", auth.max_total_debit/2, auth.max_total_debit/2)
        elif self.mode == "open":
            report = self._report(auth, "OPEN", 0, auth.max_total_debit)
        elif self.mode == "bad_total":
            report = self._report(auth, "FILLED", auth.max_total_debit+1, 0)
        else:
            report = self._report(auth, "FILLED", auth.max_total_debit, 0)
        self.install_report(auth, report)
        if self.mode == "timeout_after_fill":
            raise TimeoutError("test")
        if self.mode == "crash_after_fill":
            raise KeyboardInterrupt("simulated crash")
        return report

    def lookup_order(self, auth):
        return None if self.no_lookup else self.reports.get(auth.authorization_id)


class SystemCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.store = Tracker(Path(self.temp.name)/"state.sqlite3", clock=self.clock)
        self.broker = FakeBroker(self.clock)
        self.broker.add_market()
        self.policy = RiskPolicy()
        self.gate = GateService(self.store, self.broker, self.policy)
        self.budget = BudgetEngine(self.store, BudgetPolicy(
            analyses_per_day=100, analyses_per_cycle=100, calls_per_day=500,
            web_calls_per_day=1000, output_tokens_per_day=2_000_000, market_cooldown=0))
        self.cache = Cache(self.store)
        self.pipeline = Pipeline(self.gate, self.budget, self.cache)
        self.revision = 0

    def analyst(self, *, initial=0.64, final=0.60, confidence="high", critic=True,
                evidence_urls=(URL,), trace_urls=(URL,), decision=None):
        client = FakeClient(probabilities=(initial, final), urls=trace_urls)
        def call(market, *, budget):
            first = budget.parse(client, phase="researcher", web_calls=1,
                                 model="test-model", input="question", text_format=Forecast).output_parsed
            last = budget.parse(client, phase="critic", web_calls=1,
                                model="test-model", input="challenge", text_format=Forecast).output_parsed if critic else first
            side = decision or ("BUY_YES" if last.probability > market.probability else "BUY_NO")
            return AgentAnalysis(market_probability=market.probability,
                                 initial_probability=first.probability, final_probability=last.probability,
                                 confidence=confidence, evidence=[Evidence(title="Source", url=u, summary="Observation", supports=True) for u in evidence_urls],
                                 decision=side, reasoning="Prévision de test")
        return call

    def analysis(self, market_id="m1", **kwargs):
        if market_id not in self.broker.markets:
            self.broker.add_market(market_id)
        self.revision += 1
        return self.pipeline.analyse(market_id, self.analyst(**kwargs), cycle_id="cycle", agent_revision=f"test-{self.revision}")

    def approve(self, **kwargs):
        a = self.analysis(**kwargs)
        d = self.gate.review(a)
        self.assertTrue(d.approved, d)
        return a, d, d.authorization

    def reject(self, changes=None, code=None, **kwargs):
        a = self.analysis(**kwargs)
        if changes:
            self.broker.markets["m1"] = replace(self.broker.markets["m1"], **changes)
        d = self.gate.review(a)
        self.assertFalse(d.approved, d)
        if code:
            self.assertIn(code, d.reason_codes)
        self.assertEqual(self.broker.place_calls, 0)
        return d


class RiskTests(SystemCase):
    def test_yes_maximum_and_price_limit(self):
        _, d, auth = self.approve()
        self.assertEqual(auth.max_total_debit, 20)
        self.assertEqual(auth.amount, 19)
        self.assertEqual(auth.limit_probability, 0.52)
        self.assertEqual(auth.outcome, "YES")
        self.assertEqual(d.execution_state, "RESERVED")
        self.assertEqual(self.broker.place_calls, 0)

    def test_no_negative_raw_edge_is_valid(self):
        _, _, auth = self.approve(initial=.27, final=.25)
        self.assertEqual(auth.outcome, "NO")
        self.assertEqual(auth.limit_probability, .33)

    def test_wrong_direction_not_fixed_by_absolute_value(self):
        self.reject(initial=.27, final=.25, decision="BUY_YES", code="WRONG_DIRECTION")

    def test_skip_is_not_a_bet(self):
        self.reject(decision="SKIP", code="AGENT_SKIP")

    def test_low_confidence(self):
        self.reject(confidence="low", code="LOW_CONFIDENCE")

    def test_medium_is_exploratory(self):
        self.assertEqual(self.approve(confidence="medium")[2].max_total_debit, 5)

    def test_partial_trace_is_standard(self):
        self.assertEqual(self.approve(evidence_urls=(URL, "https://example.org/other"))[2].max_total_debit, 10)

    def test_empty_evidence_rejected(self):
        self.reject(evidence_urls=(), code="NO_EVIDENCE")

    def test_untraced_evidence_rejected(self):
        self.reject(trace_urls=(), code="NO_SOURCE_TRACE")

    def test_critic_missing_is_not_faked(self):
        _, _, auth = self.approve(initial=.6, critic=False)
        self.assertEqual(auth.max_total_debit, 5)

    def test_large_revision_caps(self):
        self.assertEqual(self.approve(initial=.8, final=.6)[2].max_total_debit, 5)

    def test_side_flip_caps(self):
        self.assertEqual(self.approve(initial=.3, final=.6)[2].max_total_debit, 5)

    def test_exceptional_disagreement_caps(self):
        self.assertEqual(self.approve(initial=.86, final=.85)[2].max_total_debit, 5)

    def test_non_mana(self):
        self.reject(changes={"token": "CASH"}, code="UNSUPPORTED_MARKET")

    def test_non_binary(self):
        self.reject(changes={"outcome_type": "MULTIPLE_CHOICE"}, code="UNSUPPORTED_MARKET")

    def test_closed(self):
        self.reject(changes={"is_closed": True}, code="MARKET_CLOSED")

    def test_resolved(self):
        self.reject(changes={"is_resolved": True}, code="MARKET_CLOSED")

    def test_missing_close_time(self):
        self.reject(changes={"close_time": None}, code="MISSING_CLOSE_TIME")

    def test_conditions_changed(self):
        self.reject(changes={"description": "New resolution rule"}, code="CONDITIONS_CHANGED")

    def test_market_moved_both_directions(self):
        a = self.analysis()
        for p in (.56, .20):
            self.broker.markets["m1"] = replace(self.broker.markets["m1"], probability=p)
            self.assertIn("MARKET_MOVED", self.gate.review(a).reason_codes)

    def test_edge_exhausted(self):
        self.reject(changes={"probability": .53}, code="INSUFFICIENT_EDGE")

    def test_stale_analysis(self):
        a = self.analysis()
        self.clock.tick(901)
        self.assertIn("STALE_ANALYSIS", self.gate.review(a).reason_codes)

    def test_stale_market(self):
        a = self.analysis()
        self.broker.market_age = 31
        self.assertIn("STALE_MARKET", self.gate.review(a).reason_codes)

    def test_stale_portfolio(self):
        a = self.analysis()
        self.broker.portfolio_age = 31
        self.assertIn("STALE_PORTFOLIO", self.gate.review(a).reason_codes)

    def test_future_snapshot(self):
        a = self.analysis()
        self.broker.market_age = -1
        self.assertIn("STALE_MARKET", self.gate.review(a).reason_codes)

    def test_nonfinite_analysis_no_network(self):
        a = self.analysis()
        before = self.broker.preview_calls
        with self.assertRaises(ValueError):
            self.gate.review(replace(a, final_probability=float("nan")))
        self.assertEqual(self.broker.preview_calls, before)

    def test_id_collision(self):
        a = self.analysis()
        with self.assertRaises(StateConflict):
            self.gate.review(replace(a, final_probability=.7))

    def test_unmetered_analysis(self):
        a = self.analysis()
        d = self.gate.review(replace(a, analysis_id="fake", budget_session_id="fake"))
        self.assertIn("UNMETERED_ANALYSIS", d.reason_codes)

    def test_tampered_tool_trace(self):
        a = self.analysis()
        # Construire un registre propre avec trace originale, mais ne pas modifier
        # un id déjà enregistré : le collision check protège aussi ce cas.
        with self.assertRaises(StateConflict):
            self.gate.review(replace(a, traced_urls=("https://fake.example/",)))

    def test_discrete_size_cash_capacity(self):
        self.broker.cash = 33  # réserve 20 : reste 13, donc palier 10
        self.assertEqual(self.approve()[2].max_total_debit, 10)

    def test_less_than_unit_no_order(self):
        self.broker.cash = 24
        self.reject(code="NO_EXECUTABLE_SIZE")

    def test_family_concentration(self):
        self.broker.remote_orders["external"] = PortfolioOrder("external", "other", "YES", 24, 0)
        self.assertEqual(self.approve()[2].max_total_debit, 5)

    def test_existing_position_even_opposite(self):
        self.broker.remote_orders["external"] = PortfolioOrder("external", "m1", "NO", 5, 0)
        self.reject(code="EXISTING_EXPOSURE")

    def test_loss_stop(self):
        self.broker.loss_today = 20
        self.reject(code="DAILY_LOSS_STOP")

    def test_daily_turnover(self):
        self.broker.debited_today = 56
        self.reject(code="NO_EXECUTABLE_SIZE")

    def test_uncertain_fees_stop(self):
        self.broker.quote_change = lambda q: replace(q, debit_bound_guaranteed=False)
        self.reject(code="PREVIEW_UNAVAILABLE_OR_UNSAFE")

    def test_quote_cannot_change_side(self):
        self.broker.quote_change = lambda q: replace(q, outcome="NO")
        self.reject(code="PREVIEW_UNAVAILABLE_OR_UNSAFE")

    def test_quote_cannot_exceed_total_debit(self):
        self.broker.quote_change = lambda q: replace(q, total_debit_upper_bound=q.total_debit_upper_bound+1)
        self.reject(code="PREVIEW_UNAVAILABLE_OR_UNSAFE")

    def test_preview_falls_to_smaller_tier(self):
        self.broker.quote_change = lambda q: replace(q, executable=q.total_debit_upper_bound <= 10)
        self.assertEqual(self.approve()[2].max_total_debit, 10)
        self.assertEqual(self.broker.preview_calls, 2)

    def test_rounding_is_conservative(self):
        self.assertEqual(conservative_limit(.587, "YES", .08), .50)
        self.assertEqual(conservative_limit(.253, "NO", .08), .34)


class ExecutionTests(SystemCase):
    def test_execution_confirmed_then_duplicate_no_send(self):
        a, _, auth = self.approve()
        row = self.gate.execute(auth.authorization_id)
        self.assertEqual(row["state"], "FILLED")
        self.assertEqual(row["spent"], units(20))
        self.gate.execute(auth.authorization_id)
        duplicate = self.gate.review(a)
        self.assertEqual(duplicate.authorization.authorization_id, auth.authorization_id)
        self.assertEqual(duplicate.execution_state, "FILLED")
        self.assertEqual(self.broker.place_calls, 1)

    def test_duplicate_review_reserves_once(self):
        a, _, auth = self.approve()
        duplicate = self.gate.review(a)
        self.assertEqual(duplicate.authorization.authorization_id, auth.authorization_id)
        self.assertEqual(len(self.store.orders()), 1)
        self.assertEqual(self.broker.preview_calls, 1)

    def test_expired_authorization_cannot_send(self):
        _, _, auth = self.approve()
        self.clock.tick(16)
        self.assertEqual(self.gate.execute(auth.authorization_id)["state"], "EXPIRED")
        self.assertEqual(self.broker.place_calls, 0)

    def test_market_change_before_send_cancels(self):
        _, _, auth = self.approve()
        self.broker.markets["m1"] = replace(self.broker.markets["m1"], probability=.54)
        self.assertEqual(self.gate.execute(auth.authorization_id)["state"], "CANCELLED")
        self.assertEqual(self.broker.place_calls, 0)

    def test_pause_prevents_send(self):
        _, _, auth = self.approve()
        self.store.pause("TEST")
        self.assertEqual(self.gate.execute(auth.authorization_id)["state"], "CANCELLED")
        self.assertEqual(self.broker.place_calls, 0)

    def test_timeout_after_send_no_retry(self):
        _, _, auth = self.approve()
        self.broker.mode = "timeout_after_fill"
        row = self.gate.execute(auth.authorization_id)
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(row["remaining"], units(20))
        self.gate.execute(auth.authorization_id)
        self.assertEqual(self.broker.place_calls, 1)
        self.assertEqual(self.gate.reconcile(auth.authorization_id)["state"], "FILLED")
        self.assertEqual(self.broker.place_calls, 1)

    def test_unknown_blocks_other_markets(self):
        _, _, auth = self.approve()
        self.broker.mode = "timeout_no_fill"
        self.gate.execute(auth.authorization_id)
        other = self.analysis("m2")
        self.assertIn("UNRESOLVED_ORDER", self.gate.review(other).reason_codes)
        with self.assertRaises(StateConflict):
            self.store.resume()

    def test_no_lookup_result_keeps_reservation(self):
        _, _, auth = self.approve()
        self.broker.mode = "timeout_no_fill"
        self.gate.execute(auth.authorization_id)
        self.clock.tick(100)
        self.gate.recover_pending()
        row = self.store.order(auth.authorization_id)
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(row["remaining"], units(20))

    def test_restart_after_crash(self):
        _, _, auth = self.approve()
        self.broker.mode = "crash_after_fill"
        with self.assertRaises(KeyboardInterrupt):
            self.gate.execute(auth.authorization_id)
        self.assertEqual(self.store.order(auth.authorization_id)["state"], "SENDING")
        reopened = Tracker(self.store.path, clock=self.clock)
        service = GateService(reopened, self.broker, self.policy)
        service.recover_pending()
        self.assertEqual(reopened.order(auth.authorization_id)["state"], "FILLED")
        self.assertEqual(self.broker.place_calls, 1)

    def test_partial_fill_and_cancellation(self):
        _, _, auth = self.approve()
        self.broker.mode = "partial"
        row = self.gate.execute(auth.authorization_id)
        self.assertEqual((row["spent"], row["remaining"]), (units(10), units(10)))
        self.clock.tick()
        report = self.broker._report(auth, "CANCELLED", 10, 0)
        self.broker.install_report(auth, report)
        row = self.gate.reconcile(auth.authorization_id)
        self.assertEqual(row["state"], "CANCELLED")
        self.assertEqual(row["spent"], units(10))
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(self.store.metrics()["open_committed_mana"], 10)

    def test_open_order_not_expired_locally(self):
        _, _, auth = self.approve()
        self.broker.mode = "open"
        self.gate.execute(auth.authorization_id)
        self.clock.tick(60)
        self.gate.recover_pending()
        self.assertEqual(self.store.order(auth.authorization_id)["remaining"], units(20))

    def test_broker_rejection_releases_reservation(self):
        _, _, auth = self.approve()
        self.broker.mode = "reject"
        row = self.gate.execute(auth.authorization_id)
        self.assertEqual((row["state"], row["remaining"]), ("REJECTED", 0))

    def test_over_debit_report_is_not_accepted(self):
        _, _, auth = self.approve()
        self.broker.mode = "bad_total"
        self.assertEqual(self.gate.execute(auth.authorization_id)["state"], "UNKNOWN")

    def test_acknowledged_fill_not_counted_twice(self):
        _, _, auth = self.approve()
        self.gate.execute(auth.authorization_id)
        cap = consolidate_portfolio(self.broker.fetch_portfolio(), self.store.orders(), self.policy, now=self.clock())
        self.assertEqual(cap.total, 20)
        self.assertEqual(cap.cash_after_pending, 80)
        self.assertEqual(cap.daily_debit_and_pending, 20)

    def test_unacknowledged_fill_still_counted(self):
        _, _, auth = self.approve()
        self.gate.execute(auth.authorization_id)
        self.broker.lagging_portfolio = True
        cap = consolidate_portfolio(self.broker.fetch_portfolio(), self.store.orders(), self.policy, now=self.clock())
        self.assertEqual(cap.total, 20)
        self.assertEqual(cap.cash_after_pending, 80)
        self.assertEqual(cap.daily_debit_and_pending, 20)

    def test_concurrent_reviews_do_not_overspend(self):
        self.broker.cash = 35  # 15 après réserve ; deux paliers 10 ne passent pas
        a, b = self.analysis(), self.analysis("m2")
        self.broker.preview_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(self.gate.review, [a, b]))
        self.assertGreaterEqual(sum(d.approved for d in decisions), 1)
        self.assertLessEqual(sum(r["remaining"] for r in self.store.orders()), units(15))

    def test_concurrent_execute_once(self):
        _, _, auth = self.approve()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.gate.execute, [auth.authorization_id]*2))
        self.assertEqual(self.broker.place_calls, 1)


class BudgetTests(SystemCase):
    def session(self, policy=None, ident="a", market="m", cycle="cycle"):
        engine = BudgetEngine(self.store, policy or BudgetPolicy())
        return engine, engine.begin(analysis_id=ident, market_id=market, cycle_id=cycle)

    def call(self, session, client=None, **kwargs):
        return session.parse(client or FakeClient(), phase="researcher", model="test", input="test", text_format=Forecast, **kwargs)

    def test_provider_limits_and_retries(self):
        _, session = self.session()
        client = FakeClient()
        self.call(session, client, web_calls=2, max_output_tokens=100_000)
        self.assertEqual(client.options[0]["max_retries"], 0)
        self.assertEqual(client.calls[0]["max_tool_calls"], 2)
        self.assertEqual(client.calls[0]["max_output_tokens"], 4096)
        self.assertIn("web_search_call.action.sources", client.calls[0]["include"])

    def test_failed_call_consumes_budget(self):
        engine, session = self.session()
        with self.assertRaises(TimeoutError):
            self.call(session, FakeClient(error=TimeoutError()))
        self.assertEqual(engine.usage()["calls"], 1)
        self.assertEqual(engine.usage()["web_calls_reserved"], 1)

    def test_web_analysis_limit_before_call(self):
        _, session = self.session(BudgetPolicy(web_calls_per_analysis=1))
        client = FakeClient()
        self.call(session, client)
        with self.assertRaises(BudgetExceeded) as cm:
            self.call(session, client)
        self.assertEqual(cm.exception.code, "ANALYSIS_WEB_CALLS")
        self.assertEqual(len(client.calls), 1)

    def test_call_count_limit(self):
        _, session = self.session(BudgetPolicy(calls_per_analysis=1))
        self.call(session)
        with self.assertRaises(BudgetExceeded) as cm:
            self.call(session)
        self.assertEqual(cm.exception.code, "ANALYSIS_CALLS")

    def test_daily_output_limit(self):
        _, session = self.session(BudgetPolicy(output_tokens_per_day=4096))
        self.call(session)
        with self.assertRaises(BudgetExceeded) as cm:
            self.call(session)
        self.assertEqual(cm.exception.code, "DAILY_OUTPUT_TOKENS")

    def test_daily_analysis_limit(self):
        engine, session = self.session(BudgetPolicy(analyses_per_day=1))
        engine.finish(session.analysis_id, success=True)
        with self.assertRaises(BudgetExceeded) as cm:
            engine.begin(analysis_id="b", market_id="other", cycle_id="next")
        self.assertEqual(cm.exception.code, "DAILY_ANALYSES")

    def test_cycle_limit(self):
        engine, session = self.session(BudgetPolicy(analyses_per_cycle=1))
        with self.assertRaises(BudgetExceeded) as cm:
            engine.begin(analysis_id="b", market_id="other", cycle_id="cycle")
        self.assertEqual(cm.exception.code, "CYCLE_ANALYSES")

    def test_cooldown_survives_restart(self):
        p = BudgetPolicy()
        engine, session = self.session(p)
        engine.finish(session.analysis_id, success=True)
        engine = BudgetEngine(Tracker(self.store.path, clock=self.clock), p)
        with self.assertRaises(BudgetExceeded) as cm:
            engine.begin(analysis_id="b", market_id="m", cycle_id="next")
        self.assertEqual(cm.exception.code, "MARKET_COOLDOWN")

    def test_duplicate_analysis_no_second_charge(self):
        engine, _ = self.session()
        with self.assertRaises(BudgetExceeded) as cm:
            engine.begin(analysis_id="a", market_id="m", cycle_id="cycle")
        self.assertEqual(cm.exception.code, "ANALYSIS_ALREADY_STARTED")
        self.assertEqual(engine.usage()["analyses"], 1)

    def test_inflight_session_blocks_same_market(self):
        engine, _ = self.session(BudgetPolicy(market_cooldown=0))
        with self.assertRaises(BudgetExceeded) as cm:
            engine.begin(analysis_id="b", market_id="m", cycle_id="next")
        self.assertEqual(cm.exception.code, "MARKET_ANALYSIS_RUNNING")

    def test_finished_session_cannot_call(self):
        engine, session = self.session()
        engine.finish(session.analysis_id, success=True)
        with self.assertRaises(BudgetExceeded):
            self.call(session)

    def test_disallow_other_tools(self):
        _, session = self.session()
        with self.assertRaises(ValueError):
            self.call(session, tools=[{"type": "function", "name": "place_bet"}])

    def test_disallow_hidden_context(self):
        _, session = self.session()
        with self.assertRaises(ValueError):
            self.call(session, previous_response_id="old")

    def test_model_allowlist(self):
        _, session = self.session(BudgetPolicy(allowed_models=("approved",)))
        with self.assertRaises(BudgetExceeded) as cm:
            self.call(session)
        self.assertEqual(cm.exception.code, "MODEL_NOT_ALLOWED")

    def test_clock_rollback_blocks_session(self):
        _, session = self.session()
        self.clock.tick(-1)
        with self.assertRaises(BudgetExceeded):
            self.call(session)

    def test_new_day_separate_counters_old_calls_retained(self):
        engine, session = self.session()
        self.call(session)
        old_day = engine.usage()["day_utc"]
        engine.finish(session.analysis_id, success=True)
        self.clock.tick(86400)
        self.assertEqual(engine.usage()["calls"], 0)
        self.assertEqual(engine.usage(old_day)["calls"], 1)

    def test_concurrent_daily_limit_atomic(self):
        engine = BudgetEngine(self.store, BudgetPolicy(analyses_per_day=1))
        def begin(i):
            try:
                engine.begin(analysis_id=str(i), market_id=str(i), cycle_id=str(i))
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(begin, [1, 2]))
        self.assertEqual(result.count(True), 1)


class CacheTrackerPipelineTests(SystemCase):
    def test_cache_no_rejuvenation(self):
        at = self.clock()
        self.cache.put("research", "k", {"x": 1}, created_at=at, ttl=10)
        self.clock.tick(5)
        self.assertEqual(self.cache.get("research", "k").created_at, at)
        self.clock.tick(5)
        self.assertIsNone(self.cache.get("research", "k"))

    def test_cache_persists(self):
        self.cache.put("research", "k", [1], created_at=self.clock(), ttl=10)
        reopened = Cache(Tracker(self.store.path, clock=self.clock))
        self.assertEqual(reopened.get("research", "k").value, [1])

    def test_cache_does_not_store_orders(self):
        with self.assertRaises(ValueError):
            self.cache.put("authorization", "k", {}, created_at=self.clock(), ttl=10)

    def test_analysis_cache_keeps_identity_and_budget(self):
        analyst = self.analyst()
        a = self.pipeline.analyse("m1", analyst, cycle_id="c", agent_revision="v1")
        before = self.budget.usage()
        self.clock.tick(5)
        b = self.pipeline.analyse("m1", analyst, cycle_id="c", agent_revision="v1")
        self.assertEqual(a, b)
        self.assertEqual(self.budget.usage(), before)

    def test_cache_version_changes_key(self):
        a = self.pipeline.analyse("m1", self.analyst(), cycle_id="c", agent_revision="v1")
        b = self.pipeline.analyse("m1", self.analyst(), cycle_id="c", agent_revision="v2")
        self.assertNotEqual(a.analysis_id, b.analysis_id)

    def test_brier_none_without_resolution(self):
        self.analysis()
        self.assertIsNone(self.store.metrics()["brier"]["final"])

    def test_brier_first_forecast_per_market_even_skipped(self):
        a = self.analysis(decision="SKIP")
        self.gate.review(a)
        self.clock.tick()
        self.analysis(final=.9)
        self.clock.tick()
        self.store.resolve_market("m1", "YES", resolved_at=self.clock(), evidence_ref="fake-resolution")
        metrics = self.store.metrics()
        self.assertEqual(metrics["n_resolved_markets"], 1)
        self.assertAlmostEqual(metrics["brier"]["market"], .58**2)
        self.assertAlmostEqual(metrics["brier"]["final"], .4**2)
        self.assertEqual(metrics["brier"]["critic_vs_researcher"]["n"], 1)

    def test_cancelled_resolution_not_scored(self):
        self.analysis()
        self.clock.tick()
        self.store.resolve_market("m1", "CANCEL", resolved_at=self.clock(), evidence_ref="cancel")
        self.assertEqual(self.store.metrics()["n_resolved_markets"], 0)

    def test_forecast_after_resolution_excluded(self):
        self.store.resolve_market("m1", "YES", resolved_at=self.clock(), evidence_ref="resolved")
        self.clock.tick()
        self.analysis()
        self.assertEqual(self.store.metrics()["n_resolved_markets"], 0)

    def test_realized_pnl_requires_confirmed_payment(self):
        _, _, auth = self.approve()
        self.gate.execute(auth.authorization_id)
        self.assertEqual(self.store.metrics()["n_settled_positions"], 0)
        self.clock.tick()
        self.store.settle(auth.authorization_id, payout=30, settled_at=self.clock(), evidence_ref="payment")
        self.assertEqual(self.store.metrics()["realized_pnl_mana"], 10)
        self.assertEqual(self.store.metrics()["open_committed_mana"], 0)
        with self.assertRaises(StateConflict):
            self.store.settle(auth.authorization_id, payout=31, settled_at=self.clock(), evidence_ref="different")

    def test_losses_trigger_family_cooldown(self):
        _, _, auth = self.approve()
        self.gate.execute(auth.authorization_id)
        self.clock.tick()
        self.store.settle(auth.authorization_id, payout=10, settled_at=self.clock(), evidence_ref="loss")
        a = self.analysis("m2")
        self.assertIn("LOSS_COOLDOWN", self.gate.review(a).reason_codes)

    def test_tracker_stores_forecast_edge_even_for_skip(self):
        a = self.analysis(decision="SKIP")
        self.gate.review(a)
        self.assertAlmostEqual(self.store.analysis(a.analysis_id)["edge"], .18)
        self.assertEqual(AnalysisEnvelope.from_dict(self.store.analysis(a.analysis_id)), a)

    def test_dashboard_distinguishes_proposal_authorization_execution(self):
        a, d, auth = self.approve()
        dashboard = self.store.dashboard()
        self.assertEqual(dashboard["analyses"][0]["decision"], "BUY_YES")
        self.assertEqual(dashboard["decisions"][0]["status"], "APPROVE")
        self.assertEqual(dashboard["orders"][0]["state"], "RESERVED")
        json.dumps(dashboard, allow_nan=False)

    def test_full_cycle_without_execution(self):
        result = self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="c", agent_revision="v")
        self.assertEqual(result[0]["decision"]["status"], "APPROVE")
        self.assertIsNone(result[0]["execution"])
        self.assertEqual(self.broker.place_calls, 0)

    def test_full_cycle_with_fake_execution(self):
        result = self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="c", agent_revision="v", execute=True)
        self.assertEqual(result[0]["execution"]["state"], "FILLED")
        self.assertEqual(self.broker.place_calls, 1)

    def test_old_unmodified_A_signature_is_not_silently_used(self):
        def old_agent(market):
            raise AssertionError("Should never reach body")
        result = self.pipeline.run_cycle(["m1"], old_agent, cycle_id="c", agent_revision="old")
        self.assertEqual(result[0]["error"], "TypeError")
        self.assertEqual(self.broker.place_calls, 0)

    def test_money_rejects_nan_and_booleans(self):
        for value in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(ValueError):
                units(value)

    def test_state_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                self.store.set_meta("test", "x", db)
                raise RuntimeError()
        with self.store.connection() as db:
            self.assertIsNone(self.store.meta("test", db))


class AdditionalSafetyTests(SystemCase):
    def test_sub_micro_cash_cannot_round_up_into_a_tier(self):
        self.broker.cash = 24.9999999
        self.reject(code="NO_EXECUTABLE_SIZE")

    def test_policy_change_cannot_reset_limits(self):
        self.approve()
        other = self.analysis("m2")
        self.gate.policy = replace(self.policy, max_total_mana=1000)
        self.assertIn("INVALID_SNAPSHOT_OR_POLICY", self.gate.review(other).reason_codes)

    def test_budget_policy_change_rejected(self):
        self.analysis()
        changed = BudgetEngine(self.store, BudgetPolicy(analyses_per_day=10000))
        with self.assertRaises(StateConflict):
            changed.begin(analysis_id="new", market_id="m2", cycle_id="c")

    def test_nan_portfolio_cannot_preview(self):
        a = self.analysis()
        self.broker.cash = float("nan")
        self.assertIn("SNAPSHOT_UNAVAILABLE", self.gate.review(a).reason_codes)
        self.assertEqual(self.broker.preview_calls, 0)

    def test_required_critic_missing(self):
        self.gate.policy = replace(self.policy, require_critic=True)
        self.reject(initial=.6, critic=False, code="CRITIC_REQUIRED")

    def test_expired_token_is_not_reissued_from_cached_analysis(self):
        a, _, auth = self.approve()
        self.clock.tick(16)
        again = self.gate.review(a)
        self.assertEqual(again.authorization.authorization_id, auth.authorization_id)
        self.assertEqual(again.execution_state, "EXPIRED")
        self.assertEqual(len(self.store.orders()), 1)
        self.assertEqual(self.gate.execute(auth.authorization_id)["state"], "EXPIRED")

    def test_refused_forecast_can_be_rechecked_if_price_changes(self):
        a = self.analysis()
        self.broker.markets["m1"] = replace(self.broker.markets["m1"], probability=.53)
        self.assertFalse(self.gate.review(a).approved)
        self.broker.markets["m1"] = replace(self.broker.markets["m1"], probability=.42)
        self.assertTrue(self.gate.review(a).approved)
        self.assertEqual(len(self.store.orders()), 1)

    def test_unmatched_local_fill_adds_to_external_daily_spending(self):
        _, _, auth = self.approve()
        self.gate.execute(auth.authorization_id)
        # Snapshot retardé : 7 Mana d'autres dépenses mais pas encore les 20 locaux.
        snapshot = PortfolioSnapshot("test-account", 93, self.clock(), (), 7, 0)
        cap = consolidate_portfolio(snapshot, self.store.orders(), self.policy, now=self.clock())
        self.assertEqual(cap.daily_debit_and_pending, 27)
        self.assertEqual(cap.cash_after_pending, 73)

    def test_partial_acknowledgement_uses_more_conservative_view(self):
        _, _, auth = self.approve()
        self.gate.execute(auth.authorization_id)
        old = PortfolioOrder("bet-"+auth.authorization_id, "m1", "YES", 10, 0)
        snapshot = PortfolioSnapshot("test-account", 90, self.clock(), (old,), 10, 0)
        cap = consolidate_portfolio(snapshot, self.store.orders(), self.policy, now=self.clock())
        self.assertEqual(cap.total, 20)
        self.assertEqual(cap.cash_after_pending, 80)
        self.assertEqual(cap.daily_debit_and_pending, 20)

    def test_zero_debit_filled_report_is_unknown(self):
        _, _, auth = self.approve()
        self.broker.place_order = lambda order: ExecutionReport(
            "FILLED", 0, 0, "bad-bet", self.clock(), "fake")
        self.assertEqual(self.gate.execute(auth.authorization_id)["state"], "UNKNOWN")

    def test_future_report_is_unknown(self):
        _, _, auth = self.approve()
        self.broker.place_order = lambda order: ExecutionReport(
            "FILLED", 20, 0, "bad-bet", self.clock()+10, "fake")
        self.assertEqual(self.gate.execute(auth.authorization_id)["state"], "UNKNOWN")

    def test_partial_report_cannot_erase_confirmed_fill(self):
        _, _, auth = self.approve()
        self.broker.mode = "partial"
        self.gate.execute(auth.authorization_id)
        self.clock.tick()
        self.broker.reports[auth.authorization_id] = self.broker._report(auth, "REJECTED", 0, 0)
        row = self.gate.reconcile(auth.authorization_id)
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(row["spent"], units(10))
        self.assertEqual(row["remaining"], units(10))

    def test_pending_reservation_survives_midnight(self):
        _, _, auth = self.approve()
        self.broker.mode = "open"
        self.gate.execute(auth.authorization_id)
        self.clock.tick(86400)
        self.broker.debited_today = 0
        cap = consolidate_portfolio(self.broker.fetch_portfolio(), self.store.orders(), self.policy, now=self.clock())
        self.assertEqual(cap.daily_debit_and_pending, 20)

    def test_fake_critic_trace_rejected_on_first_registration(self):
        a = self.analysis(critic=False, initial=.6)
        with self.budget.begin(analysis_id="fresh", market_id="m1", cycle_id="c") as session:
            session.parse(FakeClient(), phase="researcher", model="test", input="x", text_format=Forecast)
        forged = replace(a, analysis_id="fresh", budget_session_id="fresh", critic_completed=True)
        self.assertIn("TRACE_MISMATCH", self.gate.review(forged).reason_codes)

    def test_fake_source_trace_rejected_on_first_registration(self):
        a = self.analysis(critic=False, initial=.6)
        with self.budget.begin(analysis_id="fresh", market_id="m1", cycle_id="c") as session:
            session.parse(FakeClient(), phase="researcher", model="test", input="x", text_format=Forecast)
        forged = replace(a, analysis_id="fresh", budget_session_id="fresh", traced_urls=("https://fake.org/",))
        self.assertIn("TRACE_MISMATCH", self.gate.review(forged).reason_codes)

    def test_incomplete_openai_response_is_not_a_successful_analysis(self):
        with self.assertRaises(RuntimeError):
            with self.budget.begin(analysis_id="incomplete", market_id="m1", cycle_id="c") as session:
                client = FakeClient()
                client.parse = lambda **kw: FakeResponse(.6, [URL], status="incomplete")
                session.parse(client, phase="researcher", model="test", input="x", text_format=Forecast)
        self.assertEqual(self.budget.trace("incomplete")["state"], "FAILED")
        self.assertEqual(self.budget.usage()["calls"], 1)

    def test_provider_exceeding_web_reservation_pauses(self):
        client = FakeClient()
        response = FakeResponse(.6, [URL])
        payload = response.model_dump()
        payload["output"] = payload["output"]*2
        response.model_dump = lambda **kw: payload
        client.parse = lambda **kw: response
        with self.assertRaises(RuntimeError):
            with self.budget.begin(analysis_id="over", market_id="m1", cycle_id="c") as session:
                session.parse(client, phase="researcher", web_calls=1, model="test", input="x", text_format=Forecast)
        with self.store.connection() as db:
            self.assertEqual(self.store.meta("paused", db), "WEB_TOOL_LIMIT_VIOLATION")

    def test_session_limit_remains_after_settlement(self):
        self.gate.policy = replace(self.policy, max_session_debit=20)
        _, _, auth = self.approve()
        self.gate.execute(auth.authorization_id)
        self.clock.tick()
        self.store.settle(auth.authorization_id, payout=30, settled_at=self.clock(), evidence_ref="profit")
        row = self.broker.remote_orders["bet-"+auth.authorization_id]
        self.broker.remote_orders[row.external_id] = replace(row, position_open=False)
        other = self.analysis("m2")
        self.assertIn("NO_EXECUTABLE_SIZE", self.gate.review(other).reason_codes)



class BatchOptimizerTests(unittest.TestCase):
    """Comparer notamment le solveur à une énumération indépendante."""

    def setUp(self):
        from tools.risk import PortfolioCapacity
        self.cap = PortfolioCapacity(100, {}, {}, 0, 0, 0, 0)
        self.policy = RiskPolicy(max_total_mana=10, max_family_mana=100)

    def option(self, name, cost, gain, *, market=None):
        from tools.risk import AllocationOption
        market = market or name
        return AllocationOption(name, market, market, self.policy.family_for(market), cost, gain)

    def solve(self, options, **kw):
        from tools.risk import allocate_batch
        return allocate_batch(options, kw.get("capacity", self.cap), kw.get("policy", self.policy))

    def test_beats_first_come_first_served(self):
        plan = self.solve([self.option("early", 10, 5), self.option("second", 5, 4), self.option("third", 5, 4)])
        self.assertEqual({o.option_id for o in plan.selected}, {"second", "third"})
        self.assertEqual(plan.expected_gain, 8)
        self.assertEqual(plan.total_reserved, 10)

    def test_only_one_size_per_market(self):
        plan = self.solve([self.option("small", 5, 4, market="m"), self.option("large", 10, 7, market="m")])
        self.assertEqual(len(plan.selected), 1)
        self.assertEqual(plan.selected[0].option_id, "large")

    def test_zero_and_negative_gains_do_not_force_spending(self):
        plan = self.solve([self.option("bad", 5, -1), self.option("zero", 5, 0)])
        self.assertFalse(plan.selected)
        self.assertEqual(plan.total_reserved, 0)

    def test_family_constraint(self):
        self.policy = replace(self.policy, max_family_mana=5)
        plan = self.solve([self.option("a", 5, 2), self.option("b", 5, 3)])
        self.assertEqual(plan.selected[0].option_id, "b")

    def test_cash_reserve_daily_and_session_constraints(self):
        for cap in [replace(self.cap, cash_after_pending=25),
                    replace(self.cap, daily_debit_and_pending=55),
                    replace(self.cap, session_debit_and_pending=195)]:
            plan = self.solve([self.option("a", 5, 1), self.option("b", 10, 5)], capacity=cap)
            self.assertEqual(plan.total_reserved, 5)

    def test_tie_prefers_less_capital(self):
        plan = self.solve([self.option("large", 10, 5), self.option("small", 5, 5)])
        self.assertEqual(plan.selected[0].option_id, "small")

    def test_input_order_does_not_change_selection(self):
        import itertools
        options = [self.option("a", 10, 5), self.option("b", 10, 5), self.option("c", 5, 1)]
        outputs = {tuple(o.option_id for o in self.solve(list(p)).selected) for p in itertools.permutations(options)}
        self.assertEqual(len(outputs), 1)

    def test_expected_gain_no_uses_complement(self):
        from tools.risk import expected_net_gain
        self.assertAlmostEqual(expected_net_gain(.35, "NO", payout_if_correct=20,
                               payout_if_incorrect=0, estimated_debit=10), 3)
        self.assertAlmostEqual(expected_net_gain(.35, "YES", payout_if_correct=20,
                               payout_if_incorrect=0, estimated_debit=10), -3)

    def test_gain_uses_gross_payout_not_only_edge(self):
        from tools.risk import expected_net_gain
        self.assertAlmostEqual(expected_net_gain(.6, "YES", payout_if_correct=25,
                               payout_if_incorrect=0, estimated_debit=11), 4)

    def test_invalid_payout_rejected(self):
        from tools.risk import expected_net_gain
        for payout in [None, -1, float("nan"), float("inf")]:
            with self.assertRaises((ValueError, TypeError)):
                expected_net_gain(.6, "YES", payout_if_correct=payout,
                                  payout_if_incorrect=0, estimated_debit=10)

    def test_more_than_ten_markets_rejected(self):
        with self.assertRaises(ValueError):
            self.solve([self.option(str(i), 5, 1) for i in range(11)])

    def test_more_than_three_tiers_rejected(self):
        with self.assertRaises(ValueError):
            self.solve([self.option(str(i), i+1, i+1, market="same") for i in range(4)])

    def test_multiple_analyses_for_same_market_rejected(self):
        a = self.option("a", 5, 1)
        b = replace(a, option_id="b", analysis_id="other")
        with self.assertRaises(ValueError):
            self.solve([a, b])

    def test_cannot_override_family_from_analysis(self):
        with self.assertRaises(ValueError):
            self.solve([replace(self.option("a", 5, 1), family_id="invented")])

    def test_existing_position_and_cooldown_not_reopened(self):
        for cap in [replace(self.cap, by_market={"a": 1}), replace(self.cap, recent_loss_families=("UNKNOWN",))]:
            self.assertFalse(self.solve([self.option("a", 5, 2)], capacity=cap).selected)

    def test_paused_or_unknown_allocates_nothing(self):
        for cap in [replace(self.cap, paused_reason="stop"), replace(self.cap, unresolved_order=True)]:
            self.assertFalse(self.solve([self.option("a", 5, 2)], capacity=cap).selected)

    def test_random_instances_match_independent_bruteforce(self):
        import itertools
        import random
        rng = random.Random(237)
        from tools.risk import allocate_batch
        for trial in range(50):
            markets = ["m"+str(i) for i in range(5)]
            policy = replace(self.policy, max_total_mana=rng.randint(5, 25), max_family_mana=rng.randint(5, 15),
                             families=tuple((m, "f"+str(i % 2)) for i, m in enumerate(markets)))
            self.policy = policy
            groups = [[self.option(f"{m}-{j}", cost, rng.randint(-2, 15), market=m)
                       for j, cost in enumerate((5, 10, 20))] for m in markets]
            best_gain, best_cost = 0, 0
            for selection in itertools.product(*[[None]+g for g in groups]):
                chosen = [x for x in selection if x and x.expected_gain > 0]
                cost = sum(x.debit_cap for x in chosen)
                families = {f: sum(x.debit_cap for x in chosen if x.family_id == f) for f in ("f0", "f1")}
                if cost > policy.max_total_mana or any(v > policy.max_family_mana for v in families.values()):
                    continue
                gain = sum(x.expected_gain for x in chosen)
                if gain > best_gain or (gain == best_gain and cost < best_cost):
                    best_gain, best_cost = gain, cost
            plan = allocate_batch([x for group in groups for x in group], self.cap, policy)
            self.assertEqual((plan.expected_gain, plan.total_reserved), (best_gain, best_cost), trial)


class BatchGateTests(SystemCase):
    def test_collect_before_any_preview(self):
        for i in range(10):
            self.broker.add_market(f"m{i}")
        original = self.broker.preview_order
        def preview(**kwargs):
            with self.store.connection() as db:
                count = db.execute("SELECT count(*) FROM d_analyses").fetchone()[0]
            self.assertEqual(count, 10)
            return original(**kwargs)
        self.broker.preview_order = preview
        result = self.pipeline.run_cycle([f"m{i}" for i in range(10)], self.analyst(),
                                         cycle_id="ten", agent_revision="v")
        self.assertEqual(len(result), 10)
        self.assertEqual(self.budget.usage()["analyses"], 10)
        self.assertLessEqual(sum(r["remaining"] for r in self.store.orders()), units(30))
        self.assertEqual(self.broker.place_calls, 0)

    def test_joint_capacity_not_two_individual_maxima(self):
        a, b = self.analysis("m1"), self.analysis("m2")
        batch = self.gate.review_batch([a, b], batch_id="joint")
        self.assertEqual(batch.total_reserved, 30)
        self.assertEqual(sorted(d.authorization.max_total_debit for d in batch.decisions if d.approved), [10, 20])

    def test_batch_preserves_hard_stops(self):
        a, b = self.analysis("m1", confidence="low"), self.analysis("m2")
        batch = self.gate.review_batch([a, b], batch_id="hard")
        decisions = {d.analysis_id: d for d in batch.decisions}
        self.assertFalse(decisions[a.analysis_id].approved)
        self.assertIn("LOW_CONFIDENCE", decisions[a.analysis_id].reason_codes)
        self.assertTrue(decisions[b.analysis_id].approved)

    def test_medium_tier_remains_five_in_a_batch(self):
        a = self.analysis(confidence="medium")
        batch = self.gate.review_batch([a], batch_id="medium")
        self.assertEqual(batch.total_reserved, 5)

    def test_quotes_without_payout_are_not_allocated(self):
        a = self.analysis()
        self.broker.quote_change = lambda q: replace(q, payout_if_correct=None)
        result = self.gate.review_batch([a], batch_id="no-value")
        self.assertEqual(result.total_reserved, 0)
        self.assertIn("NO_VALUED_OPTION", result.decisions[0].reason_codes)

    def test_partial_fill_estimate_not_extrapolated_to_full_amount(self):
        a = self.analysis()
        self.broker.quote_change = lambda q: replace(q, full_fill_estimated=False)
        result = self.gate.review_batch([a], batch_id="partial-estimate")
        self.assertFalse(result.decisions[0].approved)

    def test_net_negative_quote_is_not_selected(self):
        a = self.analysis()
        self.broker.quote_change = lambda q: replace(q, payout_if_correct=q.estimated_debit)
        result = self.gate.review_batch([a], batch_id="negative")
        self.assertEqual(result.expected_gain, 0)
        self.assertEqual(result.total_reserved, 0)
        self.assertIn("NO_POSITIVE_FRESH_OPTION", result.decisions[0].reason_codes)

    def test_price_change_during_preview_requires_new_quotes(self):
        a = self.analysis()
        def move(q):
            self.broker.markets["m1"] = replace(self.broker.markets["m1"], probability=.43)
            return q
        self.broker.quote_change = move
        result = self.gate.review_batch([a], batch_id="moved")
        self.assertFalse(result.decisions[0].approved)
        self.assertFalse(self.store.orders())

    def test_old_analysis_does_not_get_timestamp_of_batch(self):
        a = self.analysis("m1")
        self.clock.tick(901)
        b = self.analysis("m2")
        result = self.gate.review_batch([a, b], batch_id="stale")
        decisions = {d.analysis_id: d for d in result.decisions}
        self.assertIn("STALE_ANALYSIS", decisions[a.analysis_id].reason_codes)
        self.assertTrue(decisions[b.analysis_id].approved)

    def test_replay_same_batch_is_idempotent(self):
        a = self.analysis()
        first = self.gate.review_batch([a], batch_id="same")
        calls = self.broker.preview_calls
        second = self.gate.review_batch([a], batch_id="same")
        self.assertEqual(first, second)
        self.assertEqual(self.broker.preview_calls, calls)
        self.assertEqual(len(self.store.orders()), 1)

    def test_replay_does_not_renew_expired_authorization(self):
        a = self.analysis()
        first = self.gate.review_batch([a], batch_id="exp")
        self.clock.tick(16)
        second = self.gate.review_batch([a], batch_id="exp")
        self.assertEqual(second.decisions[0].execution_state, "EXPIRED")
        self.assertEqual(first.decisions[0].authorization, second.decisions[0].authorization)
        self.assertEqual(len(self.store.orders()), 1)

    def test_new_batch_does_not_double_same_analysis(self):
        a = self.analysis()
        self.gate.review_batch([a], batch_id="first")
        second = self.gate.review_batch([a], batch_id="second")
        self.assertEqual(second.total_reserved, 0)
        self.assertEqual(len(self.store.orders()), 1)

    def test_batch_id_collision(self):
        a, b = self.analysis("m1"), self.analysis("m2")
        self.gate.review_batch([a], batch_id="collision")
        with self.assertRaises(StateConflict):
            self.gate.review_batch([b], batch_id="collision")

    def test_duplicate_market_rejected_before_allocation(self):
        a, b = self.analysis("m1"), self.analysis("m1")
        with self.assertRaises(ValueError):
            self.gate.review_batch([a, b], batch_id="duplicate")
        self.assertFalse(self.store.orders())

    def test_existing_reservations_reduce_new_batch_budget(self):
        self.approve()
        b = self.analysis("m2")
        result = self.gate.review_batch([b], batch_id="with-existing")
        self.assertEqual(result.total_reserved, 10)

    def test_batch_reservation_rolls_back_as_a_whole(self):
        from unittest.mock import patch
        a, b = self.analysis("m1"), self.analysis("m2")
        original = self.store.reserve
        calls = []
        def fail_second(decision, db):
            calls.append(decision)
            original(decision, db)
            if len(calls) == 2:
                raise RuntimeError("simulated database failure")
        with patch.object(self.store, "reserve", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.gate.review_batch([a, b], batch_id="atomic")
        self.assertFalse(self.store.orders())
        self.assertIsNone(self.store.batch("atomic"))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM d_decisions").fetchone()[0], 0)

    def test_concurrent_batches_cannot_overspend(self):
        from concurrent.futures import ThreadPoolExecutor
        analyses = [self.analysis(f"m{i}") for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(self.gate.review_batch, [a], batch_id=f"concurrent-{i}") for i,a in enumerate(analyses)]
            for job in jobs:
                job.result()
        self.assertLessEqual(sum(r["remaining"] for r in self.store.orders()), units(30))

    def test_timeout_on_first_order_blocks_later_orders(self):
        self.broker.add_market("m2")
        self.broker.mode = "timeout_after_fill"
        self.broker.no_lookup = True
        results = self.pipeline.run_cycle(["m1", "m2"], self.analyst(), cycle_id="timeout", agent_revision="v", execute=True)
        self.assertEqual(self.broker.place_calls, 1)
        self.assertIn("UNKNOWN", {r["execution"]["state"] for r in results if r.get("execution")})
        self.assertIn("CANCELLED", {r["execution"]["state"] for r in results if r.get("execution")})

    def test_execute_rechecks_gain_and_never_resizes_order(self):
        a = self.analysis()
        batch = self.gate.review_batch([a], batch_id="gain")
        auth = batch.decisions[0].authorization
        self.broker.quote_change = lambda q: replace(q, payout_if_correct=q.payout_if_correct*.9)
        result = self.gate.execute(auth.authorization_id)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(self.broker.place_calls, 0)

    def test_execute_rejects_changed_exact_amount(self):
        a = self.analysis()
        batch = self.gate.review_batch([a], batch_id="amount")
        auth = batch.decisions[0].authorization
        self.broker.quote_change = lambda q: replace(q, amount=q.amount-1)
        result = self.gate.execute(auth.authorization_id)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(self.broker.place_calls, 0)

    def test_partial_execution_keeps_residual_reserved(self):
        a = self.analysis()
        batch = self.gate.review_batch([a], batch_id="partial")
        self.broker.mode = "partial"
        row = self.gate.execute(batch.decisions[0].authorization.authorization_id)
        self.assertEqual(row["state"], "PARTIAL")
        self.assertEqual(row["spent"]+row["remaining"], units(20))

    def test_collection_deadline_closes_short_batch(self):
        self.gate.batch_policy = replace(self.gate.batch_policy, max_collection_seconds=2)
        self.broker.add_market("m2")
        original = self.analyst()
        def slow(market, *, budget):
            result = original(market, budget=budget)
            self.clock.tick(3)
            return result
        results = self.pipeline.run_cycle(["m1", "m2"], slow, cycle_id="deadline", agent_revision="v")
        self.assertEqual(self.budget.usage()["analyses"], 1)
        self.assertIn("COLLECTION_DEADLINE", [r.get("error") for r in results])

    def test_cycle_replay_does_not_call_A_again(self):
        self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="replay-cycle", agent_revision="v")
        before = self.budget.usage()
        self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="replay-cycle", agent_revision="v")
        self.assertEqual(self.budget.usage(), before)
        self.assertEqual(len(self.store.orders()), 1)

    def test_cycle_cannot_change_request_under_same_id(self):
        self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="request", agent_revision="v")
        with self.assertRaises(StateConflict):
            self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="request", agent_revision="different")

    def test_expired_during_solver_creates_no_authorization(self):
        from unittest.mock import patch
        from tools.risk import allocate_batch
        a = self.analysis()
        def slow_solver(*args, **kwargs):
            plan = allocate_batch(*args, **kwargs)
            self.clock.tick(31)
            return plan
        with patch("tools.integration.allocate_batch", side_effect=slow_solver):
            result = self.gate.review_batch([a], batch_id="slow-solve")
        self.assertFalse(result.optimal_for_supplied_options)
        self.assertEqual(result.total_reserved, 0)
        self.assertFalse(self.store.orders())

    def test_empty_batch_is_a_valid_noop(self):
        result = self.gate.review_batch([], batch_id="empty")
        self.assertEqual(result.total_reserved, 0)
        self.assertFalse(result.decisions)


class LogbookTests(SystemCase):
    def test_a_b_d_events_share_batch_and_analysis(self):
        results = self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="log", agent_revision="v")
        events = self.store.logbook(batch_id="log")
        self.assertTrue({"A", "B", "D"}.issubset({e["component"] for e in events}))
        aid = results[0]["decision"]["analysis_id"]
        phases = [e for e in events if e["operation"] == "OPENAI_CALL" and e["status"] == "SUCCEEDED"]
        self.assertEqual({e["payload"]["phase"] for e in phases}, {"researcher", "critic"})
        self.assertTrue(all(e["analysis_id"] == aid for e in phases))

    def test_start_end_and_duration_are_recorded(self):
        with self.store.operation("B", "TEST_FETCH", batch_id="dur") as log:
            self.clock.tick(2)
            log["snapshot_at"] = self.clock()
        rows = self.store.logbook(batch_id="dur")
        self.assertEqual([r["status"] for r in rows], ["STARTED", "SUCCEEDED"])
        self.assertEqual(rows[0]["operation_id"], rows[1]["operation_id"])
        self.assertGreaterEqual(rows[1]["payload"]["duration_ms"], 0)

    def test_failed_operations_keep_start_and_error_type_not_exception_text(self):
        with self.assertRaises(RuntimeError):
            with self.store.operation("B", "FAIL", batch_id="failure"):
                raise RuntimeError("sensitive text")
        rows = self.store.logbook(batch_id="failure")
        self.assertEqual(rows[-1]["status"], "FAILED")
        self.assertNotIn("sensitive text", json.dumps(rows))

    def test_recorded_time_is_distinct_from_observed_time(self):
        at = self.clock()-60
        self.store.log_event("B", "OLD_FACT", occurred_at=at)
        row = self.store.logbook()[-1]
        self.assertEqual(row["occurred_at"], at)
        self.assertEqual(row["recorded_at"], self.clock())
        self.assertTrue(row["timestamp_utc"].endswith("+00:00"))

    def test_future_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.log_event("B", "BAD_TIME", occurred_at=self.clock()+1)

    def test_cache_hit_does_not_rejuvenate(self):
        self.cache.put("research", "k", {"value": 1}, created_at=self.clock(), ttl=100)
        original = self.clock()
        self.clock.tick(5)
        self.cache.get("research", "k")
        row = self.store.logbook()[-1]
        self.assertEqual(row["operation"], "CACHE_HIT")
        self.assertEqual(row["payload"]["original_created_at"], original)
        self.assertEqual(row["recorded_at"], original+5)

    def test_observed_web_trace_does_not_invent_provider_timestamp(self):
        self.analysis()
        rows = [r for r in self.store.logbook() if r["operation"] == "WEB_TOOL_OBSERVED"]
        self.assertTrue(rows)
        self.assertTrue(all(r["payload"]["provider_timestamp_known"] is False for r in rows))

    def test_shared_api_accepts_c_events(self):
        self.store.log_event("C", "DASHBOARD_RENDERED", batch_id="c", payload={"rows": 10})
        row = self.store.logbook(batch_id="c")[0]
        self.assertEqual(row["component"], "C")

    def test_redaction_of_credentials_and_prompt(self):
        self.store.log_event("A", "METADATA", payload={"api_key": "dont-log-me", "prompt": "private",
            "nested": {"Authorization": "Bearer abc", "url": "https://example.org/?token=secret"},
            "authorization": {"authorization_id": "safe-id"}})
        data = json.dumps(self.store.logbook()[-1])
        for forbidden in ("dont-log-me", "Bearer abc", "token=secret", '"private"'):
            self.assertNotIn(forbidden, data)
        self.assertIn("safe-id", data)

    def test_jsonl_export_and_pagination(self):
        for i in range(7):
            self.store.log_event("C", "PAGE", batch_id="pages", payload={"i": i})
        first = self.store.logbook(batch_id="pages", limit=3)
        second = self.store.logbook(batch_id="pages", after_seq=first[-1]["seq"], limit=3)
        self.assertEqual([r["payload"]["i"] for r in first+second], list(range(6)))
        path = Path(self.temp.name)/"log.jsonl"
        self.assertEqual(self.store.export_logbook(path, batch_id="pages"), 7)
        self.assertEqual(len(path.read_text().splitlines()), 7)

    def test_database_restart_preserves_log_and_budget(self):
        self.analysis()
        before = self.store.logbook()
        other = Tracker(self.store.path, clock=self.clock)
        after = other.logbook()
        self.assertEqual(after[:len(before)], before)
        self.assertNotEqual(other.run_id, self.store.run_id)
        self.assertEqual(BudgetEngine(other, self.budget.policy).usage(), self.budget.usage())

    def test_additive_tables_do_not_reset_old_ledger(self):
        _, _, auth = self.approve()
        before = self.budget.usage()
        with self.store.connection() as db:
            db.execute("DROP TABLE d_logbook")
            db.execute("DROP TABLE d_batches")
        other = Tracker(self.store.path, clock=self.clock)
        self.assertEqual(other.order(auth.authorization_id)["state"], "RESERVED")
        self.assertEqual(BudgetEngine(other, self.budget.policy).usage(), before)

    def test_dashboard_exposes_batch_plan_and_log(self):
        self.pipeline.run_cycle(["m1"], self.analyst(), cycle_id="dashboard", agent_revision="v")
        data = self.store.dashboard()
        self.assertEqual(data["batches"][0]["batch_id"], "dashboard")
        self.assertTrue(data["logbook"])
        json.dumps(data, allow_nan=False)



class V2RegressionTests(SystemCase):
    def test_changed_batch_policy_cannot_relax_existing_authorization(self):
        a = self.analysis()
        batch = self.gate.review_batch([a], batch_id="policy")
        self.gate.batch_policy = replace(self.gate.batch_policy, max_quote_age=100)
        row = self.gate.execute(batch.decisions[0].authorization.authorization_id)
        self.assertEqual(row["state"], "CANCELLED")
        self.assertEqual(self.broker.place_calls, 0)

    def test_cycle_reports_markets_beyond_its_limit(self):
        self.gate.batch_policy = replace(self.gate.batch_policy, max_analyses=1)
        self.broker.add_market("m2")
        rows = self.pipeline.run_cycle(["m1", "m2"], self.analyst(), cycle_id="limited", agent_revision="v")
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(r for r in rows if r["market_id"] == "m2")["error"], "BATCH_LIMIT")

    def test_direct_analysis_fetch_is_correlated_with_cycle(self):
        self.analysis()
        fetches = [r for r in self.store.logbook(batch_id="cycle") if r["operation"] == "FETCH_MARKET"]
        self.assertTrue(fetches)
        self.assertTrue(all(r["market_id"] == "m1" for r in fetches))

    def test_failed_a_call_is_logged_and_does_not_create_authorization(self):
        client = FakeClient(error=TimeoutError("do not repeat"))
        def failing(market, *, budget):
            return budget.parse(client, phase="researcher", model="test-model", input="x", text_format=Forecast)
        self.pipeline.run_cycle(["m1"], failing, cycle_id="failed-a", agent_revision="v")
        rows = [r for r in self.store.logbook(batch_id="failed-a") if r["operation"] == "OPENAI_CALL"]
        self.assertEqual([r["status"] for r in rows], ["STARTED", "FAILED"])
        self.assertFalse(self.store.orders())
        self.assertEqual(self.budget.usage()["calls"], 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
