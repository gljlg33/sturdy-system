"""Daily summary: decisions made, trades taken, PnL after fees, every risk veto that fired.

Usage: python -m desk.reports.daily --log-dir var/logs --day 2026-10-18
"""
from __future__ import annotations

import argparse
import time
from collections import Counter

from desk.logs.store import EventLog


def summarize(events) -> dict:
    decisions = Counter()
    fills, vetoes, intents, rejections = [], [], Counter(), Counter()
    for e in events:
        k = e["kind"]
        if k == "decision":
            decisions[e["action"]] += 1
        elif k == "fill":
            fills.append(e)
        elif k == "veto":
            vetoes.append(e)
        elif k == "intent":
            intents[e["mode"]] += 1
        elif k == "rejection":
            rejections[e["check"]] += 1

    # Realized cash flow after fees and gas, on closed round trips only.
    cash, fees, pos = 0.0, 0.0, {}
    realized = 0.0
    for f in fills:
        sign = 1 if f["side"] == "buy" else -1
        cash -= sign * f["qty"] * f["price"]
        fees += f["fee"] + f["gas"]
        pos[f["symbol"]] = pos.get(f["symbol"], 0.0) + sign * f["qty"]
        if all(abs(q) < 1e-12 for q in pos.values()):
            realized, cash = realized + cash, 0.0
    return {
        "decisions": sum(decisions.values()),
        "decisions_by_action": dict(decisions),
        "intents_by_mode": dict(intents),
        "trades": len(fills),
        "fees_and_gas": fees,
        "realized_pnl_after_fees": realized - fees,
        "open_after_day": {s: q for s, q in pos.items() if abs(q) > 1e-12},
        "rejections_by_check": dict(rejections),
        "vetoes": [{"ts": v["ts"], "symbol": v["symbol"], "check": v["check"], "detail": v["detail"]} for v in vetoes],
    }


def to_markdown(day: str, s: dict) -> str:
    out = [
        f"# Daily summary {day}", "",
        f"- decisions: {s['decisions']} {s['decisions_by_action']}",
        f"- would-trade intents by mode: {s['intents_by_mode']}",
        f"- trades (paper fills): {s['trades']}",
        f"- fees + gas: {s['fees_and_gas']:.2f}",
        f"- realized PnL after fees: {s['realized_pnl_after_fees']:.2f}",
        f"- open at end of day: {s['open_after_day'] or 'none'}",
        f"- funnel rejections: {s['rejections_by_check']}", "",
        f"## Risk vetoes ({len(s['vetoes'])})", "",
    ]
    if s["vetoes"]:
        out += ["| time (UTC) | symbol | check | detail |", "|---|---|---|---|"]
        out += [f"| {time.strftime('%H:%M:%S', time.gmtime(v['ts']))} | {v['symbol']} | {v['check']} | {v['detail']} |"
                for v in s["vetoes"]]
    else:
        out.append("none")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default="var/logs")
    ap.add_argument("--day", default=time.strftime("%Y-%m-%d", time.gmtime()))
    args = ap.parse_args()
    print(to_markdown(args.day, summarize(EventLog(args.log_dir).read(args.day))))


if __name__ == "__main__":
    main()
