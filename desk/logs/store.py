"""Append-only JSONL event log: snapshots, questions, answers (with probabilities and
model version), decisions, vetoes, orders, fills and outcomes. One file per UTC day.

Secrets are scrubbed before anything is written: any key that looks like a secret is
replaced, and known secret values from the environment are masked wherever they appear.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

_SECRET_KEY = re.compile(r"(secret|token|api[_-]?key|authorization|password|private)", re.I)
_SECRET_ENV = ("DESK_SECRET", "JEV_API_KEY")


def _scrub(obj: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        return {k: ("[redacted]" if _SECRET_KEY.search(str(k)) else _scrub(v, secrets)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(x, secrets) for x in obj]
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "[redacted]")
        return obj
    return obj


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class EventLog:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, ts: float) -> Path:
        return self.root / f"{time.strftime('%Y-%m-%d', time.gmtime(ts))}.jsonl"

    def write(self, kind: str, ts: float, **fields: Any) -> dict:
        secrets = tuple(v for k in _SECRET_ENV if len(v := os.environ.get(k, "")) >= 8)
        rec = _scrub({"kind": kind, "ts": ts, **fields}, secrets)
        line = json.dumps(rec, separators=(",", ":"), default=str)
        with self._lock, self._path(ts).open("a") as f:
            f.write(line + "\n")
        return rec

    def read(self, day: str | None = None) -> Iterator[dict]:
        files = [self.root / f"{day}.jsonl"] if day else sorted(self.root.glob("*.jsonl"))
        for p in files:
            if p.exists():
                with p.open() as f:
                    for line in f:
                        if line.strip():
                            yield json.loads(line)
