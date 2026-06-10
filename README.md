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
