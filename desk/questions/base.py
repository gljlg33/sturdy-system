"""Question battery primitives.

Rules enforced at import time:
- one judgment per question (a question has exactly one subject and one answer set)
- every Choice question includes an "other" option
- every question names how its outcome is resolved, so calibration can be measured later
"""
from __future__ import annotations

from dataclasses import dataclass, field

OTHER = "other"


@dataclass(frozen=True)
class Choice:
    id: str
    text: str
    options: tuple[str, ...]
    # How the realized answer is determined after the fact (see desk/logs/outcomes.py).
    resolver: str
    horizon_s: int

    def __post_init__(self):
        if OTHER not in self.options:
            raise ValueError(f"question {self.id!r}: every Choice needs an {OTHER!r} option")
        if len(set(self.options)) != len(self.options):
            raise ValueError(f"question {self.id!r}: duplicate options")
        if " and " in self.text.lower() or self.text.count("?") != 1:
            raise ValueError(f"question {self.id!r}: ask exactly one judgment per question")

    def to_wire(self) -> dict:
        return {"id": self.id, "text": self.text, "options": list(self.options)}


@dataclass(frozen=True)
class Battery:
    bot: str
    questions: tuple[Choice, ...] = field(default_factory=tuple)

    def __post_init__(self):
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError(f"battery {self.bot!r}: duplicate question ids")

    def to_wire(self) -> list[dict]:
        return [q.to_wire() for q in self.questions]

    def get(self, qid: str) -> Choice:
        return next(q for q in self.questions if q.id == qid)
