from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

from .auto_trader import AutoTrader
from .config import AppConfig, public_config
from .futu_client import FutuPaperClient, _load_futu
from .models import OrderIntent
from .news_signals import load_news_signals
from .watchlist import load_watchlist
from .web_server import run_web_server


def _jsonable(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _print_json(payload: Any) -> None:
    print(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, default=str))


def cmd_config(_: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    _print_json(public_config(config))
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    checks: list[dict[str, Any]] = []

    try:
        futu = _load_futu(config.use_system_home)
        checks.append(
            {
                "name": "futu-api import",
                "ok": True,
                "details": {
                    "ret_ok": futu.RET_OK,
                    "trd_env": futu.TrdEnv.SIMULATE,
                    "markets": [futu.TrdMarket.US, futu.TrdMarket.HK, futu.TrdMarket.CN],
                },
            }
        )
    except Exception as exc:
        checks.append({"name": "futu-api import", "ok": False, "error": str(exc)})

    try:
        with socket.create_connection((config.opend_host, config.opend_port), timeout=2.0):
            pass
        checks.append({"name": "OpenD TCP port", "ok": True, "target": f"{config.opend_host}:{config.opend_port}"})
    except OSError as exc:
        checks.append(
            {
                "name": "OpenD TCP port",
                "ok": False,
                "target": f"{config.opend_host}:{config.opend_port}",
                "error": str(exc),
            }
        )

    config_payload = public_config(config)
    checks.append({"name": "paper-only mode", "ok": True, "details": "orders use TrdEnv.SIMULATE only"})
    _print_json({"ok": all(check["ok"] for check in checks), "config": config_payload, "checks": checks})
    return 0 if all(check["ok"] for check in checks) else 2


def cmd_validate(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    intent = OrderIntent.from_file(args.intent)
    decision = FutuPaperClient(config).validate(intent)
    _print_json({"ok": decision.approved, "intent": intent.to_dict(), "violations": decision.violations})
    return 0 if decision.approved else 2


def cmd_place(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    intent = OrderIntent.from_file(args.intent)
    result = FutuPaperClient(config).place_order(intent, execute=args.execute)
    _print_json(result)
    return 0 if result.get("ok") else 2


def cmd_snapshot(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    result = FutuPaperClient(config).snapshot(args.codes)
    _print_json(result)
    return 0 if result.get("ok") else 2


def cmd_account(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    result = FutuPaperClient(config).account(args.market, args.currency)
    _print_json(result)
    return 0 if result.get("ok") else 2


def cmd_positions(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    result = FutuPaperClient(config).positions(args.market)
    _print_json(result)
    return 0 if result.get("ok") else 2


def cmd_watchlist(args: argparse.Namespace) -> int:
    markets = {market.upper() for market in args.market} if args.market else None
    items = load_watchlist(markets=markets)
    _print_json({"ok": True, "count": len(items), "items": [item.__dict__ for item in items]})
    return 0


def cmd_news_signals(_: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    payload = load_news_signals(config.news)
    _print_json(payload)
    return 0 if payload.get("ok") else 2


def cmd_ai_once(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    execute = False if args.dry_run else args.execute or (config.gemini.auto_enabled and config.gemini.auto_execute)
    result = AutoTrader(config).run_once(execute=execute, notes=args.note or [])
    _print_json(result.__dict__)
    return 0 if result.ok else 2


def cmd_ai_loop(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    if not config.gemini.auto_enabled and not args.force:
        print("Gemini auto loop is disabled. Set GEMINI_AUTO_ENABLED=true or pass --force.", file=sys.stderr)
        return 2
    execute = False if args.dry_run else args.execute or (config.gemini.auto_enabled and config.gemini.auto_execute)
    AutoTrader(config).loop(execute=execute)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    run_web_server(args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="futu-paper-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("config", help="Show effective configuration")
    config_parser.set_defaults(func=cmd_config)

    doctor_parser = subparsers.add_parser("doctor", help="Check SDK import and OpenD connectivity")
    doctor_parser.set_defaults(func=cmd_doctor)

    validate_parser = subparsers.add_parser("validate", help="Validate an order intent JSON file")
    validate_parser.add_argument("--intent", required=True, help="Path to order intent JSON")
    validate_parser.set_defaults(func=cmd_validate)

    place_parser = subparsers.add_parser("place", help="Dry-run or execute a paper order")
    place_parser.add_argument("--intent", required=True, help="Path to order intent JSON")
    place_parser.add_argument("--execute", action="store_true", help="Submit to Futu paper trading")
    place_parser.set_defaults(func=cmd_place)

    snapshot_parser = subparsers.add_parser("snapshot", help="Get market snapshots from OpenD")
    snapshot_parser.add_argument("codes", nargs="+", help="Futu codes, e.g. US.AAPL HK.00700 SH.600519")
    snapshot_parser.set_defaults(func=cmd_snapshot)

    account_parser = subparsers.add_parser("account", help="Get simulated account funds")
    account_parser.add_argument("--market", required=True, choices=["US", "HK", "CN"])
    account_parser.add_argument("--currency", default="HKD", help="Display currency, e.g. USD, HKD, CNH")
    account_parser.set_defaults(func=cmd_account)

    positions_parser = subparsers.add_parser("positions", help="Get simulated positions")
    positions_parser.add_argument("--market", required=True, choices=["US", "HK", "CN"])
    positions_parser.set_defaults(func=cmd_positions)

    watchlist_parser = subparsers.add_parser("watchlist", help="Show the default AI watchlist")
    watchlist_parser.add_argument("--market", action="append", choices=["US", "HK", "CN"])
    watchlist_parser.set_defaults(func=cmd_watchlist)

    news_parser = subparsers.add_parser("news-signals", help="Show recent autoNews signals passed to Gemini")
    news_parser.set_defaults(func=cmd_news_signals)

    ai_once_parser = subparsers.add_parser("ai-once", help="Run one Gemini decision cycle")
    ai_once_parser.add_argument("--execute", action="store_true", help="Allow simulated execution if checks pass")
    ai_once_parser.add_argument("--dry-run", action="store_true", help="Force dry-run even when auto execute is enabled")
    ai_once_parser.add_argument("--note", action="append", help="Optional news/source note to pass to Gemini")
    ai_once_parser.set_defaults(func=cmd_ai_once)

    ai_loop_parser = subparsers.add_parser("ai-loop", help="Run Gemini decision cycles continuously")
    ai_loop_parser.add_argument("--execute", action="store_true", help="Allow simulated execution if checks pass")
    ai_loop_parser.add_argument("--dry-run", action="store_true", help="Force dry-run even when auto execute is enabled")
    ai_loop_parser.add_argument("--force", action="store_true", help="Run even if GEMINI_AUTO_ENABLED=false")
    ai_loop_parser.set_defaults(func=cmd_ai_loop)

    web_parser = subparsers.add_parser("web", help="Start the local web console")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8787)
    web_parser.set_defaults(func=cmd_web)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
