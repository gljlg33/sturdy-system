"""Backends the judge service can call. Only the judge process imports this module.

TypeSafeJevBackend uses the official TypeSafe Python SDK (`typesafe-sdk`, pinned in
pyproject.toml). The SDK is imported lazily so the desk itself stays stdlib-only.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol


class RateLimited(Exception):
    def __init__(self, retry_after_s: float | None = None):
        super().__init__("rate limited")
        self.retry_after_s = retry_after_s


class MalformedQuestion(Exception):
    """The provider rejected the question battery itself. Never retried."""


class BackendError(Exception):
    pass


@dataclass(frozen=True)
class BackendResult:
    model_version: str                       # exact version string reported by the provider response
    answers: dict[str, dict[str, float]]     # question id -> option -> probability, as returned
    raw: Any = None


class JevBackend(Protocol):
    def ask_battery(self, *, context: str, questions: list[dict]) -> BackendResult:
        """Ask the whole battery in ONE provider call."""
        ...


class TypeSafeJevBackend:
    def __init__(self, api_key: str, model: str, timeout_s: float = 10.0, http_client=None):
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        self.model = model
        # The SDK must not retry on its own: the desk client knows the block deadline and does the
        # backing off. max_retries=0 also guarantees a malformed question is sent exactly once.
        # The key lives only inside the SDK client (its config keeps it out of repr).
        self._client = TypeSafeClient(api_key=api_key, model=model, retry=RetryPolicy(max_retries=0),
                                      timeout=timeout_s, http_client=http_client)

    def __repr__(self) -> str:  # never let the key show up in a traceback or log line
        return f"TypeSafeJevBackend(model={self.model!r})"

    def available_models(self) -> list[str]:
        return [m.name for m in self._client.models.list().models]

    def ask_battery(self, *, context: str, questions: list[dict]) -> BackendResult:
        from typesafe_sdk import (Choice, TypeSafeBadRequestError, TypeSafeError, TypeSafeRateLimitError,
                                  TypeSafeUnprocessableEntityError)

        qs = {q["id"]: Choice(instructions=q["text"], criteria=q.get("criteria") or {o: None for o in q["options"]})
              for q in questions}
        try:
            resp = self._client.system_one(state=context, questions=qs, model=self.model)  # one call, whole battery
        except TypeSafeRateLimitError as e:
            raise RateLimited(_retry_after_s(e.headers)) from None
        except (TypeSafeBadRequestError, TypeSafeUnprocessableEntityError) as e:
            raise MalformedQuestion(str(e)) from None
        except TypeSafeError as e:
            raise BackendError(type(e).__name__) from None
        return BackendResult(
            model_version=resp.model,  # the model that actually answered, as the API reports it
            answers={qid: dict(a.probabilities) for qid, a in resp.choices.items()},
        )


def _retry_after_s(headers) -> float | None:
    raw = headers.get("retry-after-ms")
    if raw is not None:
        try:
            return max(0.0, float(raw) / 1000)
        except ValueError:
            pass
    raw = headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


class FakeJevBackend:
    """Deterministic stand-in for development and tests. Its version never matches a real
    pin, so risk/ vetoes every order that came from it."""

    model_version = "fake-jev-0"

    def ask_battery(self, *, context: str, questions: list[dict]) -> BackendResult:
        answers = {}
        for q in questions:
            h = hashlib.sha256((context + q["id"]).encode()).digest()
            w = [1 + b for b in h[: len(q["options"])]]
            tot = sum(w)
            answers[q["id"]] = {o: x / tot for o, x in zip(q["options"], w)}
        return BackendResult(self.model_version, answers, raw=None)
