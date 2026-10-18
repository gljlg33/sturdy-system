import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from desk.book.book import BENCH_SECONDS, Book, OpenPosition
from desk.broker.base import Order, Side
from desk.broker.paper import PaperBroker
from desk.config import Mode, require_pinned_model
from desk.feed import SyntheticFeed
from desk.judge.backend import BackendResult, MalformedQuestion, RateLimited
from desk.judge.client import JudgeClient, MalformedQuestionError
from desk.judge.service import serve
from desk.logs.outcomes import direction_2bps
from desk.logs.store import EventLog
from desk.policy.policy import THRESHOLDS, Action, decide
from desk.questions import BATTERIES
from desk.questions.base import Choice
from desk.reports.calibration import calibrate
from desk.reports.daily import summarize
from desk.risk.vetoes import RiskCheckedBroker, RiskLimits, VetoedOrder, check_order
from desk.runner import Desk
from desk.state.snapshot import MAX_TOKENS, BookTop, EquityPoint, build_snapshot, estimate_tokens

SECRET = "s" * 40
PIN = "jev-test-2026-10-01"


def books(n=600, t0=1_000_000.0, p=100.0, step=0.0):
    return [BookTop(t0 + i, p + i * step - 0.01, p + i * step + 0.01, 2.0, 1.0) for i in range(n)]


def snap(**kw):
    bs = books(**kw)
    return build_snapshot("X", bs[-1].ts + 1, bs, [EquityPoint(bs[0].ts, 1000.0)], 0.0)


def limits(**kw):
    base = dict(max_position_notional=10_000, daily_loss_limit=500, max_leverage=1.0, range_guard_bps=10,
                fee_bps=5, gas_per_order=0, min_edge_over_costs_bps=2, pinned_model=PIN)
    base.update(kw)
    return RiskLimits(**base)


class ScriptedBackend:
    """Returns a fixed distribution per question, or raises what it's told to."""

    def __init__(self, version=PIN, lean=None, raise_=None):
        self.version, self.lean, self.raise_, self.calls = version, lean or {}, raise_, 0

    def ask_battery(self, *, context, questions):
        self.calls += 1
        if self.raise_:
            raise self.raise_
        out = {}
        for q in questions:
            opts = q["options"]
            fav = self.lean.get(q["id"])
            out[q["id"]] = {o: (0.9 if o == fav else 0.1 / (len(opts) - 1)) if fav else 1 / len(opts) for o in opts}
        return BackendResult(self.version, out)


def start_judge(backend):
    httpd = serve(backend, SECRET, PIN, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"


class TestState(unittest.TestCase):
    def test_future_data_is_ignored(self):
        bs = books()
        t = bs[300].ts
        a = build_snapshot("X", t, bs, [], 0.0)
        b = build_snapshot("X", t, bs[:300], [], 0.0)
        self.assertEqual(a, b)
        self.assertLess(a.as_of_ts, t)

    def test_fields_and_budget(self):
        s = snap(step=0.01)
        self.assertGreater(s.ret_bps[0], 0)
        self.assertAlmostEqual(s.imbalance, 1 / 3)
        self.assertLess(estimate_tokens(s.to_prompt()), MAX_TOKENS)

    def test_drawdown(self):
        bs = books()
        eq = [EquityPoint(1, 100.0), EquityPoint(2, 80.0), EquityPoint(bs[-1].ts + 10, 10.0)]
        s = build_snapshot("X", bs[-1].ts + 1, bs, eq, 0.0)
        self.assertAlmostEqual(s.drawdown_pct, 20.0)  # the later point is in the future and ignored


class TestQuestions(unittest.TestCase):
    def test_every_choice_has_other(self):
        for b in BATTERIES.values():
            for q in b.questions:
                self.assertIn("other", q.options)

    def test_rejects_missing_other_and_double_questions(self):
        with self.assertRaises(ValueError):
            Choice("x", "Up?", ("up", "down"), "direction_2bps", 60)
        with self.assertRaises(ValueError):
            Choice("x", "Up and wider?", ("up", "other"), "direction_2bps", 60)


class TestPolicy(unittest.TestCase):
    def test_thresholds_scale_with_cost(self):
        self.assertGreater(THRESHOLDS[Action.ENTER_LONG].threshold(2, 10), THRESHOLDS[Action.EXIT].threshold(2, 5))
        self.assertGreater(THRESHOLDS[Action.ENTER_LONG].threshold(8, 10), THRESHOLDS[Action.ENTER_LONG].threshold(1, 10))

    def test_strong_up_enters_long_and_other_holds(self):
        s = snap()
        up = {"gambler": {"g_dir_5m": {"up": .95, "down": .02, "flat": .02, "other": .01},
                          "g_dir_30m": {"up": .95, "down": .02, "flat": .02, "other": .01}},
              "spy": {"s_imb_follow_5m": {"follows": .95, "reverses": .02, "flat": .02, "other": .01}}}
        self.assertEqual(decide(up, s).action, Action.ENTER_LONG)
        unsure = {"gambler": {"g_dir_5m": {"up": .3, "other": .7}}}
        self.assertEqual(decide(unsure, s).action, Action.HOLD)


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.pb = PaperBroker(10_000, fee_bps=5)
        self.bt = BookTop(100.0, 99.99, 100.01, 1, 1)
        self.pb.on_book("X", self.bt)

    def order(self, **kw):
        base = dict(client_id="o", symbol="X", side=Side.BUY, qty=10, limit_price=100.01, ts=100.0,
                    expected_edge_bps=50, model_version=PIN)
        base.update(kw)
        return Order(**base)

    def checks(self, order, **lk):
        return {v.check for v in check_order(order, self.pb.account(), self.bt, limits(**lk), 100.0)}

    def test_clean_order_passes(self):
        self.assertEqual(self.checks(self.order()), set())

    def test_each_veto(self):
        self.assertIn("model_version", self.checks(self.order(model_version="jev-latest")))
        self.assertIn("range_guard", self.checks(self.order(limit_price=101)))
        self.assertIn("max_position", self.checks(self.order(qty=200)))
        self.assertIn("leverage_cap", self.checks(self.order(qty=150), max_position_notional=1e9))
        self.assertIn("fees_plus_gas", self.checks(self.order(expected_edge_bps=5)))
        self.pb.account().cash -= 600
        self.assertIn("daily_loss_limit", self.checks(self.order()))

    def test_wrapper_blocks_before_broker(self):
        vetoed = []
        rb = RiskCheckedBroker(self.pb, limits(), lambda s: self.bt, lambda: 100.0, lambda o, v: vetoed.append(v))
        with self.assertRaises(VetoedOrder):
            rb.submit_order(self.order(model_version="other"))
        self.assertEqual(self.pb.position("X"), 0)
        self.assertTrue(vetoed)
        rb.submit_order(self.order())
        self.assertEqual(self.pb.position("X"), 10)


class TestBook(unittest.TestCase):
    def test_one_position_and_bench(self):
        b = Book()
        b.open_position(OpenPosition("X", 1, 1, 100, 0, "d"))
        self.assertFalse(b.can_scan())
        with self.assertRaises(RuntimeError):
            b.open_position(OpenPosition("Y", 1, 1, 100, 0, "d"))
        b.bench("Z", "risk_veto", 0)
        self.assertIsNotNone(b.benched("Z", BENCH_SECONDS["risk_veto"] - 1))
        self.assertIsNone(b.benched("Z", BENCH_SECONDS["risk_veto"]))
        self.assertLess(BENCH_SECONDS["stale_data"], BENCH_SECONDS["risk_veto"])


class TestJudge(unittest.TestCase):
    def test_auth_and_raw_answers(self):
        be = ScriptedBackend()
        httpd, url = start_judge(be)
        try:
            bad = JudgeClient(url, lambda: "wrong" * 10)
            self.assertIsNone(bad.ask("gambler", "d", "ctx", BATTERIES["gambler"].to_wire(), time.time() + 5))
            self.assertEqual(be.calls, 0)
            res = JudgeClient(url, lambda: SECRET).ask("gambler", "d", "ctx", BATTERIES["gambler"].to_wire(), time.time() + 5)
            self.assertEqual(res.model_version, PIN)
            self.assertEqual(set(res.answers), {q.id for q in BATTERIES["gambler"].questions})
            self.assertEqual(be.calls, 1)  # whole battery in one call
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_malformed_is_never_retried(self):
        be = ScriptedBackend(raise_=MalformedQuestion("bad"))
        httpd, url = start_judge(be)
        try:
            with self.assertRaises(MalformedQuestionError):
                JudgeClient(url, lambda: SECRET).ask("spy", "d", "c", BATTERIES["spy"].to_wire(), time.time() + 5)
            self.assertEqual(be.calls, 1)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_rate_limit_backs_off_then_holds_at_deadline(self):
        be = ScriptedBackend(raise_=RateLimited())
        httpd, url = start_judge(be)
        slept = []
        try:
            c = JudgeClient(url, lambda: SECRET, sleep=lambda s: (slept.append(s), time.sleep(s)), base_backoff_s=0.05)
            self.assertIsNone(c.ask("spy", "d", "c", BATTERIES["spy"].to_wire(), time.time() + 1.0))
            self.assertGreater(be.calls, 1)
            self.assertTrue(all(b >= a * 0.5 for a, b in zip(slept, slept[1:])))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_floating_model_refused(self):
        for bad in ("jev-latest", "", "jev-2-latest"):
            with self.assertRaises(SystemExit):
                require_pinned_model(bad)
        with self.assertRaises(SystemExit):
            Mode.parse("live")


class TestLogsAndReports(unittest.TestCase):
    def test_secrets_scrubbed(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["JEV_API_KEY"] = "sk-very-secret-value"
            try:
                log = EventLog(d)
                log.write("x", 0, api_key="abc", note="key is sk-very-secret-value", nested={"Authorization": "b"})
                text = "".join(p.read_text() for p in Path(d).glob("*.jsonl"))
            finally:
                del os.environ["JEV_API_KEY"]
            self.assertNotIn("sk-very-secret-value", text)
            self.assertNotIn('"abc"', text)

    def test_outcome_waits_for_horizon(self):
        s = snap()
        later = books(n=400, t0=s.decision_ts, p=100.1)
        self.assertIsNone(direction_2bps(s, later[:100], 300))
        self.assertEqual(direction_2bps(s, later, 300), "up")

    def test_calibration_math(self):
        ev = [{"kind": "answer", "decision_id": "a", "bot": "g", "model_version": PIN, "answers": {"q": {"up": 1.0, "down": 0.0}}},
              {"kind": "outcome", "decision_id": "a", "bot": "g", "question_id": "q", "realized": "up"},
              {"kind": "answer", "decision_id": "b", "bot": "g", "model_version": PIN, "answers": {"q": {"up": 0.5, "down": 0.5}}},
              {"kind": "outcome", "decision_id": "b", "bot": "g", "question_id": "q", "realized": "down"}]
        c = calibrate(ev)
        self.assertEqual(c.n_forecasts, 4)
        self.assertAlmostEqual(c.brier, (0 + 0 + 0.25 + 0.25) / 4)
        self.assertAlmostEqual(c.brier_by_question["g/q"], (0 + 0.5) / 2)


class TestDeskEndToEnd(unittest.TestCase):
    LEAN_UP = {"g_dir_5m": "up", "g_dir_30m": "up", "s_imb_follow_5m": "follows",
               "g_jump_15m": "no", "h_adverse_1m": "neither"}

    def run_desk(self, mode, version=PIN, blocks=40):
        be = ScriptedBackend(version=version, lean=self.LEAN_UP)
        httpd, url = start_judge(be)
        d = tempfile.mkdtemp()
        try:
            desk = Desk(mode=mode, symbols=("X",), feed=SyntheticFeed(spread_bps=0.5), judge=JudgeClient(url, lambda: SECRET),
                        limits=limits(range_guard_bps=10), log=EventLog(d), block_seconds=60, order_notional=5000,
                        paper=PaperBroker(100_000, fee_bps=5))
            t0 = 1_700_000_000 - 1_700_000_000 % 86400 + 7200.5
            for i in range(blocks):
                desk.step(t0 + i * 60)
            return desk, list(EventLog(d).read())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_shadow_never_executes(self):
        desk, ev = self.run_desk(Mode.SHADOW)
        kinds = {e["kind"] for e in ev}
        self.assertIn("intent", kinds)
        self.assertNotIn("fill", kinds)
        self.assertEqual(desk.paper.account().positions, {})

    def test_paper_trades_and_logs(self):
        desk, ev = self.run_desk(Mode.PAPER)
        fills = [e for e in ev if e["kind"] == "fill"]
        self.assertTrue(fills)
        s = summarize(ev)
        self.assertEqual(s["trades"], len(fills))
        self.assertTrue(any(e["kind"] == "answer" and e["model_version"] == PIN for e in ev))

    def test_wrong_model_version_is_vetoed(self):
        desk, ev = self.run_desk(Mode.PAPER, version="jev-other-version")
        self.assertFalse([e for e in ev if e["kind"] == "fill"])
        self.assertTrue([e for e in ev if e["kind"] == "veto" and e["check"] == "model_version"])


if __name__ == "__main__":
    unittest.main()
