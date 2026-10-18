"""Desk configuration.

Secrets never live here. The desk process only ever reads DESK_SECRET (to talk
to the judge). The Jev key (TYPESAFE_API_KEY) is read by the judge service alone
(desk/judge/service.py).
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass


class Mode(str, enum.Enum):
    SHADOW = "shadow"  # Jev labels every decision, policy logs intent, nothing executes
    PAPER = "paper"    # policy executes against the paper broker

    @classmethod
    def parse(cls, raw: str) -> "Mode":
        try:
            return cls(raw.strip().lower())
        except ValueError:
            # There is deliberately no "live" mode. Anything else is refused.
            raise SystemExit(f"refusing to start: mode must be 'shadow' or 'paper', got {raw!r}")


def require_pinned_model(model: str) -> str:
    """Jev must be pinned to an exact version. Floating aliases are refused."""
    m = (model or "").strip()
    if not m or m == "jev-latest" or m.endswith("-latest") or m.endswith(":latest"):
        raise SystemExit(f"refusing to start: JEV_MODEL must be an exact pinned version, got {model!r}")
    return m


@dataclass(frozen=True)
class DeskConfig:
    mode: Mode
    judge_url: str
    pinned_model: str      # the version string every Jev response must report
    block_seconds: int     # decision cadence; a judge answer arriving after the next block is dropped
    log_dir: str
    symbols: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "DeskConfig":
        return cls(
            mode=Mode.parse(os.environ.get("DESK_MODE", "shadow")),
            judge_url=os.environ.get("JUDGE_URL", "http://127.0.0.1:8787"),
            pinned_model=require_pinned_model(os.environ.get("JEV_MODEL", "")),
            block_seconds=int(os.environ.get("BLOCK_SECONDS", "60")),
            log_dir=os.environ.get("DESK_LOG_DIR", "var/logs"),
            symbols=tuple(s for s in os.environ.get("DESK_SYMBOLS", "BTC-USD").split(",") if s),
        )


def desk_secret() -> str:
    """Shared secret between bots and the judge. Read at call time, never logged."""
    s = os.environ.get("DESK_SECRET", "")
    if len(s) < 32:
        raise SystemExit("refusing to start: DESK_SECRET must be set (>= 32 chars)")
    return s
