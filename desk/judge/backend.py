"""Backends the judge service can call. Only the judge process imports this module.

TODO(jev-sdk): TypeSafeJevBackend is a stub. Wire it to the official TypeSafe SDK once
its package name and API are confirmed; do not guess at the call shape.
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
    def __init__(self, api_key: str, model: str):
        self._api_key = api_key  # held only in the judge process memory
        self.model = model

    def __repr__(self) -> str:  # never let the key show up in a traceback or log line
        return f"TypeSafeJevBackend(model={self.model!r})"

    def ask_battery(self, *, context: str, questions: list[dict]) -> BackendResult:
        # Expected shape once wired:
        #   resp = <sdk>.ask(model=self.model, context=context, questions=questions)   # one call
        #   map the SDK's rate-limit error   -> raise RateLimited(retry_after)
        #   map the SDK's invalid-question error -> raise MalformedQuestion(detail)
        #   return BackendResult(model_version=<exact version from resp>, answers=..., raw=resp)
        raise NotImplementedError("TypeSafe SDK not wired yet; see TODO(jev-sdk) in desk/judge/backend.py")


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
