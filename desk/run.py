"""Entry point.

  python -m desk.run                     # real loop: needs DESK_MODE, JEV_MODEL, DESK_SECRET, a running judge
  python -m desk.run --demo 600 --mode paper
                                         # offline: synthetic feed, in-process judge with FakeJevBackend,
                                         # simulated clock. No keys, no network beyond localhost.
"""
from __future__ import annotations

import argparse
import secrets
import threading
import time

from desk.broker.paper import PaperBroker
from desk.config import DeskConfig, Mode, desk_secret
from desk.feed import SyntheticFeed
from desk.judge.client import JudgeClient
from desk.logs.store import EventLog
from desk.risk.vetoes import RiskLimits
from desk.runner import Desk

STARTING_CASH = 100_000.0
ORDER_NOTIONAL = 5_000.0


def default_limits(pinned_model: str) -> RiskLimits:
    return RiskLimits(
        max_position_notional=10_000.0,
        daily_loss_limit=1_000.0,
        max_leverage=1.0,
        range_guard_bps=10.0,
        fee_bps=5.0,
        gas_per_order=0.0,
        min_edge_over_costs_bps=2.0,
        pinned_model=pinned_model,
    )


def run_demo(blocks: int, mode: Mode, log_dir: str, block_seconds: int = 60) -> None:
    from desk.judge.backend import FakeJevBackend
    from desk.judge.service import serve

    secret = secrets.token_urlsafe(32)  # ephemeral, in-memory only
    pinned = FakeJevBackend.model_version  # demo pins to the fake; a real pin never matches it
    httpd = serve(FakeJevBackend(), secret, pinned, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address

    desk = Desk(
        mode=mode, symbols=("BTC-USD", "ETH-USD"), feed=SyntheticFeed(), log=EventLog(log_dir),
        judge=JudgeClient(f"http://{host}:{port}", lambda: secret), limits=default_limits(pinned),
        block_seconds=block_seconds, order_notional=ORDER_NOTIONAL, paper=PaperBroker(STARTING_CASH, fee_bps=5.0),
    )
    t0 = (int(time.time()) // 86400) * 86400 + 3 * 3600 + 0.5  # a fixed hour of today, mid-second
    for i in range(blocks):
        desk.step(t0 + i * block_seconds)
    httpd.shutdown()
    httpd.server_close()
    print(f"demo done: {blocks} blocks, mode={mode.value}, equity={desk.paper.account().equity:.2f}, logs in {log_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", type=int, metavar="BLOCKS")
    ap.add_argument("--mode", default=None)
    ap.add_argument("--log-dir", default=None)
    args = ap.parse_args()

    if args.demo:
        run_demo(args.demo, Mode.parse(args.mode or "shadow"), args.log_dir or "var/demo-logs")
        return

    DeskConfig.from_env()  # validates mode and model pin
    desk_secret()          # fail fast if missing
    # A real market-data feed plugs in here (Desk(feed=...)); it is not wired yet.
    raise SystemExit("no market-data feed configured yet: wiring an exchange feed needs your go-ahead (see README)")


if __name__ == "__main__":
    main()
