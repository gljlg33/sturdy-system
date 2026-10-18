"""TypeSafeJevBackend against a mocked HTTP transport. Skipped when typesafe-sdk isn't installed."""
import json
import unittest

try:
    import httpx2
    import typesafe_sdk  # noqa: F401
except ImportError:  # the desk itself doesn't need the SDK; only the judge does
    httpx2 = None

from desk.judge.backend import BackendError, MalformedQuestion, RateLimited, TypeSafeJevBackend
from desk.questions import BATTERIES

PIN = "jev-1.14-test"
KEY = "test-key-not-real-0000000000"


@unittest.skipIf(httpx2 is None, "typesafe-sdk not installed")
class TestTypeSafeBackend(unittest.TestCase):
    def backend(self, handler):
        self.requests = []

        def record(request):
            self.requests.append(request)
            return handler(request)

        client = httpx2.Client(transport=httpx2.MockTransport(record))
        return TypeSafeJevBackend(api_key=KEY, model=PIN, http_client=client)

    def ok(self, request):
        body = json.loads(request.content)
        answers = {qid: {"type": "choice", "choice": next(iter(q["criteria"])), "confidence": 0.5,
                         "probabilities": {o: 1 / len(q["criteria"]) for o in q["criteria"]}}
                   for qid, q in body["questions"].items()}
        return httpx2.Response(200, json={"model": PIN + "-20261014", "usage": {"input_tokens": 1, "output_tokens": 1},
                                          "answers": answers})

    def test_whole_battery_one_call_pinned_model(self):
        be = self.backend(self.ok)
        battery = BATTERIES["house"].to_wire()
        res = be.ask_battery(context="sym=X mid=1", questions=battery)
        self.assertEqual(len(self.requests), 1)
        sent = json.loads(self.requests[0].content)
        self.assertEqual(sent["model"], PIN)
        self.assertEqual(set(sent["questions"]), {q["id"] for q in battery})
        self.assertIn("other", sent["questions"]["h_spread_5m"]["criteria"])
        self.assertTrue(sent["questions"]["h_spread_5m"]["criteria"]["other"])  # described, not a bare label
        self.assertEqual(res.model_version, PIN + "-20261014")  # what answered, verbatim
        self.assertAlmostEqual(sum(res.answers["h_spread_5m"].values()), 1.0)

    def test_rate_limit_maps_without_sdk_retry(self):
        be = self.backend(lambda r: httpx2.Response(429, headers={"retry-after": "3"}, json={"error": "slow down"}))
        with self.assertRaises(RateLimited) as cm:
            be.ask_battery(context="c", questions=BATTERIES["spy"].to_wire())
        self.assertEqual(cm.exception.retry_after_s, 3.0)
        self.assertEqual(len(self.requests), 1)

    def test_malformed_never_retried(self):
        for status in (400, 422):
            be = self.backend(lambda r: httpx2.Response(status, json={"error": "bad question"}))
            with self.assertRaises(MalformedQuestion):
                be.ask_battery(context="c", questions=BATTERIES["spy"].to_wire())
            self.assertEqual(len(self.requests), 1)

    def test_server_error_is_backend_error_and_key_not_leaked(self):
        be = self.backend(lambda r: httpx2.Response(500, json={"error": "boom"}))
        with self.assertRaises(BackendError) as cm:
            be.ask_battery(context="c", questions=BATTERIES["spy"].to_wire())
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(KEY, repr(be) + str(cm.exception) + repr(cm.exception))
        self.assertEqual(self.requests[0].headers["authorization"], f"Bearer {KEY}")


if __name__ == "__main__":
    unittest.main()
