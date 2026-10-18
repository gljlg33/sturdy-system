"""Desk-side judge client. Holds only the desk secret, never the Jev key.

Deadline semantics: every call carries the start of the next block as its deadline.
If no valid answer has arrived by then, ask() returns None and the caller holds.
Rate limits back off (honouring Retry-After) inside the deadline. A malformed-question
error is raised immediately and never retried.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


class MalformedQuestionError(Exception):
    pass


@dataclass(frozen=True)
class JudgeResult:
    model_version: str
    answers: dict[str, dict[str, float]]
    latency_ms: float


class JudgeClient:
    def __init__(self, url: str, secret: Callable[[], str], clock=time.time, sleep=time.sleep,
                 base_backoff_s: float = 0.5, max_backoff_s: float = 8.0):
        self.url = url.rstrip("/") + "/v1/judge"
        self._secret = secret
        self._clock, self._sleep = clock, sleep
        self._base, self._max = base_backoff_s, max_backoff_s

    def ask(self, bot: str, decision_id: str, context: str, questions: list[dict], deadline_ts: float) -> JudgeResult | None:
        body = json.dumps({"bot": bot, "decision_id": decision_id, "context": context, "questions": questions}).encode()
        attempt = 0
        while True:
            remaining = deadline_ts - self._clock()
            if remaining <= 0:
                return None
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json", "X-Desk-Secret": self._secret()})
            try:
                with urllib.request.urlopen(req, timeout=remaining) as resp:
                    data = json.loads(resp.read())
                if self._clock() > deadline_ts:
                    return None  # arrived after the next block started: stale, hold
                return JudgeResult(data["model_version"], data["answers"], data.get("latency_ms", 0.0))
            except urllib.error.HTTPError as e:
                if e.code == 422:
                    raise MalformedQuestionError(e.read().decode(errors="replace")[:500])
                if e.code not in (429, 503):
                    return None
                retry_after = e.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else \
                    min(self._max, self._base * 2 ** attempt) * (0.5 + random.random() / 2)
                attempt += 1
                if self._clock() + wait >= deadline_ts:
                    return None
                self._sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
                return None
