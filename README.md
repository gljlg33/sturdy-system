# sturdy-system
24/7 trade assist: the skeleton of an AI trading desk. **Paper trading only. There is no live broker and no live mode.**

## Layout

| dir | what it does |
|---|---|
| `desk/state/` | raw book tops -> snapshot (mid, spread, imbalance, realized vol, 1m/5m/30m returns, inventory, drawdown). Uses only records with `ts < decision_ts`; under 400 tokens. |
| `desk/questions/` | one battery per bot: `gambler.py`, `house.py`, `spy.py`. One judgment per question; every Choice has `other`; each names how its outcome is resolved. |
| `desk/policy/` | thresholds and combination weights in code. One threshold per action = break-even probability for what being wrong costs, plus a margin. |
| `desk/risk/` | hard vetoes before every order: max position, daily loss limit, leverage cap, range guard, fees + gas, model version. `RiskCheckedBroker` is the only broker the desk gets. |
| `desk/broker/` | `Broker` interface and `PaperBroker`. Nothing else. |
| `desk/filter/` | the funnel: free cut -> one-call check -> hard facts -> Jev -> final pick. Every rejection is logged with the check that fired. |
| `desk/book/` | one open position at a time (no scan while open) and a bench whose duration depends on the rejection reason. |
| `desk/logs/` | JSONL event log (snapshots, questions, answers + probabilities + model version, decisions, vetoes, fills, outcomes) with secret scrubbing; outcome resolvers. |
| `desk/judge/` | the judge service (holds the Jev key), its backend adapter, and the desk-side client (holds only the desk secret). |
| `desk/reports/` | `calibration.py` (buckets, Brier, reliability SVG) and `daily.py` (decisions, trades, PnL after fees, vetoes). |

## Jev and the judge

- Only the judge process reads `JEV_API_KEY`. Bots call `POST /v1/judge` with `X-Desk-Secret`.
- One call per bot per decision carries the bot's full battery. The judge returns answers raw plus the exact `model_version` string; it makes no trading decision.
- `JEV_MODEL` must be an exact version. `jev-latest` and any `*-latest` are refused at startup. Every answer is logged with its version, and risk vetoes any opening order whose version isn't the pinned one.
- Deadline = start of the next block. No answer by then -> hold. 429/503 back off (honouring `Retry-After`) inside the deadline. A malformed-question error (422) is never retried.
- **TODO(jev-sdk):** `TypeSafeJevBackend` in `desk/judge/backend.py` is a stub until the official TypeSafe SDK is confirmed.

## Modes

- `shadow`: Jev labels every decision, the policy logs `intent` events, nothing executes (the book tracks a virtual position).
- `paper`: the policy executes against `PaperBroker` through the risk vetoes.

## Run

```sh
python -m unittest discover -s tests -t .           # tests, stdlib only
python -m desk.run --demo 600 --mode shadow         # offline: synthetic feed + in-process fake judge
python -m desk.reports.daily --log-dir var/demo-logs --day YYYY-MM-DD
python -m desk.reports.calibration --log-dir var/demo-logs --out var/reports
```

The real loop (`python -m desk.run`) stops with an error until a market-data feed is wired. Any exchange connection that needs a key waits for explicit approval, and that key must be trading-only with withdrawals disabled.

## Secrets

None in code or logs. Configure via environment (see `.env.example`; `.env` is git-ignored). The event log redacts secret-looking keys and masks `DESK_SECRET` / `JEV_API_KEY` values wherever they appear.
