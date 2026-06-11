# futu-paper-ai

Futu OpenAPI paper-trading helper for AI-generated order intents.

This project is intentionally paper-only. It always sends orders with
`TrdEnv.SIMULATE` and does not use any real-trading unlock password.

## What You Need To Provide

1. A Futu / Futubull account that can log in to OpenD.
2. OpenD running locally and logged in.
3. OpenD host and port. Defaults are `127.0.0.1:11111`.
4. The symbols you want the AI to trade, for example:
   - US: `US.AAPL`
   - HK: `HK.00700`
   - A-share Shanghai: `SH.600519`
   - A-share Shenzhen: `SZ.000001`
5. Risk limits: max order value, max quantity, and whitelist.
6. Later, an AI provider or signal source. For now the executor accepts a
   strict JSON order intent so the AI cannot call Futu directly.

## Setup

```bash
cd /Users/liurunsheng/Documents/futu-paper-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` before placing simulated orders.

The Futu Python SDK writes logs under `$HOME/.com.futunn.FutuOpenD/Log`.
This project defaults `HOME` to `.runtime/home` before importing the SDK, so
SDK logs stay under this project directory.

Check the local setup:

```bash
python -m futu_paper_ai doctor
```

Start the web console:

```bash
python -m futu_paper_ai web
```

Then open `http://127.0.0.1:8787`.

Run one Gemini decision cycle without simulated execution:

```bash
python -m futu_paper_ai ai-once --dry-run
```

Run one Gemini decision cycle and allow paper execution if all checks pass:

```bash
python -m futu_paper_ai ai-once --execute
```

Run continuous Gemini paper automation:

```bash
python -m futu_paper_ai ai-loop --execute
```

The auto loop observes the 100-code watchlist, selects a small candidate set
from quote snapshots, asks Gemini for BUY / SELL / HOLD with reasons, and only
submits paper orders after risk checks pass.

Gemini decisions default to `GEMINI_AGENT_MODE=multi_lite`. This keeps the
existing single-call Gemini workflow, but asks the model to fill a
TradingAgents-style research brief before the final action: market analyst,
news analyst, portfolio analyst, bull case, bear case, conservative risk
review, portfolio-manager summary, and missing data. The executor still reads
only the strict BUY / SELL / HOLD fields and applies the same paper-only risk
checks.

Local portfolios can be used for AB tests before syncing anything to Futu.
Each portfolio has an AI application mode: `observe` only records decisions,
`manual` waits for the user to apply an AI order from the decision detail page,
and `auto` applies risk-approved AI orders to the local portfolio ledger. Local
applications update `data/state/portfolios.json`, write a trade record, and do
do not submit Futu orders unless that portfolio has Futu paper sync enabled.
When sync is enabled, applying an AI order first submits a Futu SIMULATE order
and then writes the actual Futu fill quantity and average fill price back to the
local ledger. Local buys use broker-like buying power: they spend the trade
currency first and can auto-convert base-currency cash using the stored FX table
when the trade currency balance is insufficient. The app now probes Futu OpenD
FX snapshots first; if the current OpenD/account setup does not support FX
quotes, it falls back to the local HKD table and records that source in
portfolio payloads and trade logs. US stock snapshots also keep Futu's
pre-market, after-hours, and overnight fields as `extended_session` sentiment
signals; regular `last_price` / bid / ask remain the trade and valuation price
sources.

Candidate selection is two-stage:

1. Rank the 100-code watchlist by market activity:
   `abs(change_pct) * 2.8 + amplitude * 0.8 + max(volume_ratio - 1, 0) * 4 + log10(turnover) * 0.35`.
2. If recent autoNews signals match a watchlist ticker, boost that ticker into
   the candidate set so Gemini sees both the relevant news and the matching
   quote snapshot.

autoNews notes are filtered by relevance before they reach Gemini: current
candidate matches first, then watchlist matches, then high-impact macro signals.
Unmatched single-stock news is ignored.

If autoNews is running and writing its SQLite signal database, point this app at
that database with `AUTONEWS_DB_PATH` so every Gemini cycle receives recent
high-impact news notes. This value is environment-specific: use a local path on
your Mac and a server path on EC2.

```bash
AUTONEWS_DB_PATH=/Users/liurunsheng/Documents/autoNews/news.db
AUTONEWS_LOOKBACK_HOURS=24
AUTONEWS_MIN_IMPACT=60
AUTONEWS_MAX_SIGNALS=8
python -m futu_paper_ai news-signals
```

`news-signals` is read-only. It shows the autoNews items that will be appended
to Gemini's `notes` field during `ai-once` and `ai-loop`.

## Commands

Validate an AI order intent without connecting to Futu:

```bash
python -m futu_paper_ai validate --intent examples/order_intent.example.json
```

Dry-run an order. This still does not connect to Futu:

```bash
python -m futu_paper_ai place --intent examples/order_intent.example.json
```

Actually submit to Futu paper trading:

```bash
python -m futu_paper_ai place --intent examples/order_intent.example.json --execute
```

Query a market snapshot:

```bash
python -m futu_paper_ai snapshot US.AAPL HK.00700
```

Query paper account funds or positions:

```bash
python -m futu_paper_ai account --market US --currency USD
python -m futu_paper_ai positions --market HK
```

## A-share Note

The code supports `CN` paper-trading configuration, but A-share market data
requires the corresponding Futu quote permission. Keep `CN`, `SH.*`, and `SZ.*`
out of `.env` until that permission is enabled.

## Safety Defaults

- Orders are always sent with `TrdEnv.SIMULATE`.
- Gemini can only execute markets in `GEMINI_EXECUTE_MARKETS`.
- Current default executes US only; HK is observe-only because the HK paper
  account returned zero buying power.
- Market orders are disabled.
- SELL is blocked if there is no long position.
- Each Gemini cycle is logged under `data/decisions/`.

## Order Intent Contract

The AI should only output JSON like this:

```json
{
  "code": "US.AAPL",
  "side": "BUY",
  "qty": 1,
  "price": 190.5,
  "order_type": "NORMAL",
  "reason": "Example paper order"
}
```

The executor validates market, whitelist, side, order type, quantity, and
notional value before it can submit anything to the paper account.
