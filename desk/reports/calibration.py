"""Calibration report: predicted probability vs what happened.

Joins every logged answer (question -> option -> probability, plus model version)
with its resolved outcome. Each (question, option) pair becomes one binary forecast:
p = predicted probability, o = 1 if that option came true. From those:
  - reliability table: per 0.1 bucket, mean predicted vs observed frequency, count
  - Brier score (binary, over all option forecasts) and multiclass Brier per question
  - reliability chart as standalone SVG

Usage: python -m desk.reports.calibration --log-dir var/logs --out var/reports
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from desk.logs.store import EventLog

N_BUCKETS = 10


@dataclass(frozen=True)
class Bucket:
    lo: float
    hi: float
    n: int
    mean_p: float
    freq: float


@dataclass(frozen=True)
class Calibration:
    n_forecasts: int
    n_questions: int
    brier: float                          # binary, averaged over option forecasts
    brier_by_question: dict[str, float]   # multiclass, averaged over resolved instances
    buckets: list[Bucket]
    model_versions: list[str]


def collect(events) -> tuple[list[tuple[float, int]], dict[str, list[float]], set[str]]:
    answers: dict[tuple[str, str], tuple[dict, str]] = {}
    outcomes: list[dict] = []
    for e in events:
        if e["kind"] == "answer":
            answers[(e["decision_id"], e["bot"])] = (e["answers"], e["model_version"])
        elif e["kind"] == "outcome":
            outcomes.append(e)

    pairs: list[tuple[float, int]] = []
    per_q: dict[str, list[float]] = defaultdict(list)
    versions: set[str] = set()
    for o in outcomes:
        got = answers.get((o["decision_id"], o["bot"]))
        if not got:
            continue
        dists, version = got
        dist = dists.get(o["question_id"])
        if not dist:
            continue
        versions.add(version)
        total = 0.0
        for opt, p in dist.items():
            hit = 1 if opt == o["realized"] else 0
            pairs.append((float(p), hit))
            total += (float(p) - hit) ** 2
        per_q[f"{o['bot']}/{o['question_id']}"].append(total)
    return pairs, per_q, versions


def calibrate(events) -> Calibration:
    pairs, per_q, versions = collect(events)
    sums = [[0, 0.0, 0] for _ in range(N_BUCKETS)]  # n, sum_p, hits
    for p, hit in pairs:
        i = min(int(p * N_BUCKETS), N_BUCKETS - 1)
        sums[i][0] += 1
        sums[i][1] += p
        sums[i][2] += hit
    buckets = [Bucket(i / N_BUCKETS, (i + 1) / N_BUCKETS, n, sp / n if n else 0.0, h / n if n else 0.0)
               for i, (n, sp, h) in enumerate(sums)]
    brier = sum((p - h) ** 2 for p, h in pairs) / len(pairs) if pairs else float("nan")
    return Calibration(
        n_forecasts=len(pairs),
        n_questions=sum(len(v) for v in per_q.values()),
        brier=brier,
        brier_by_question={k: sum(v) / len(v) for k, v in sorted(per_q.items())},
        buckets=buckets,
        model_versions=sorted(versions),
    )


def to_markdown(c: Calibration) -> str:
    lines = [
        "# Calibration report", "",
        f"- resolved questions: {c.n_questions}",
        f"- option forecasts: {c.n_forecasts}",
        f"- Brier score (binary, lower is better): {c.brier:.4f}",
        f"- model versions: {', '.join(c.model_versions) or 'none'}", "",
        "| bucket | n | mean predicted | observed |", "|---|---:|---:|---:|",
    ]
    for b in c.buckets:
        lines.append(f"| {b.lo:.1f}-{b.hi:.1f} | {b.n} | {b.mean_p:.3f} | {b.freq:.3f} |" if b.n
                     else f"| {b.lo:.1f}-{b.hi:.1f} | 0 | - | - |")
    lines += ["", "| question | multiclass Brier |", "|---|---:|"]
    lines += [f"| {k} | {v:.4f} |" for k, v in c.brier_by_question.items()]
    return "\n".join(lines) + "\n"


def to_svg(c: Calibration, size: int = 420) -> str:
    pad, w = 48, size - 72
    x = lambda p: pad + p * w
    y = lambda p: pad / 2 + (1 - p) * w
    pts = [b for b in c.buckets if b.n]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(b.mean_p):.1f},{y(b.freq):.1f}" for i, b in enumerate(pts))
    ticks = "".join(
        f'<line class="grid" x1="{x(t / 5):.1f}" y1="{y(0):.1f}" x2="{x(t / 5):.1f}" y2="{y(1):.1f}"/>'
        f'<line class="grid" x1="{x(0):.1f}" y1="{y(t / 5):.1f}" x2="{x(1):.1f}" y2="{y(t / 5):.1f}"/>'
        f'<text class="tick" x="{x(t / 5):.1f}" y="{y(0) + 16:.1f}" text-anchor="middle">{t / 5:.1f}</text>'
        f'<text class="tick" x="{x(0) - 8:.1f}" y="{y(t / 5) + 4:.1f}" text-anchor="end">{t / 5:.1f}</text>'
        for t in range(6))
    dots = "".join(
        f'<circle class="dot" cx="{x(b.mean_p):.1f}" cy="{y(b.freq):.1f}" r="5">'
        f'<title>{b.lo:.1f}-{b.hi:.1f}: predicted {b.mean_p:.3f}, observed {b.freq:.3f}, n={b.n}</title></circle>'
        for b in pts)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" role="img" aria-label="Reliability chart">
<style>
  svg {{ --surface:#fcfcfb; --ink:#0b0b0b; --muted:#52514e; --grid:#e4e3df; --series:#2a78d6; background:var(--surface); font:12px system-ui, sans-serif; }}
  @media (prefers-color-scheme: dark) {{ svg {{ --surface:#1a1a19; --ink:#f0efec; --muted:#c3c2b7; --grid:#3a3a37; --series:#3987e5; }} }}
  .grid {{ stroke:var(--grid); stroke-width:1; }}
  .tick {{ fill:var(--muted); }}
  .ideal {{ stroke:var(--muted); stroke-width:1; stroke-dasharray:4 4; fill:none; }}
  .line {{ stroke:var(--series); stroke-width:2; fill:none; }}
  .dot {{ fill:var(--series); stroke:var(--surface); stroke-width:2; }}
  .label {{ fill:var(--ink); }}
</style>
<title>Reliability: predicted vs observed (Brier {c.brier:.4f}, n={c.n_forecasts})</title>
{ticks}
<line class="ideal" x1="{x(0):.1f}" y1="{y(0):.1f}" x2="{x(1):.1f}" y2="{y(1):.1f}"/>
<path class="line" d="{path}"/>
{dots}
<text class="label" x="{x(0.5):.1f}" y="{size - 6}" text-anchor="middle">predicted probability</text>
<text class="label" transform="translate(12 {y(0.5):.1f}) rotate(-90)" text-anchor="middle">observed frequency</text>
<text class="tick" x="{x(1):.1f}" y="{y(1) - 6:.1f}" text-anchor="end">dashed = perfectly calibrated</text>
</svg>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default="var/logs")
    ap.add_argument("--out", default="var/reports")
    args = ap.parse_args()
    c = calibrate(EventLog(args.log_dir).read())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "calibration.md").write_text(to_markdown(c))
    (out / "reliability.svg").write_text(to_svg(c))
    print(to_markdown(c))


if __name__ == "__main__":
    main()
