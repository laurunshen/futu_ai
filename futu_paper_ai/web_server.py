from __future__ import annotations

import json
import math
import mimetypes
import socket
from collections import deque
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auto_trader import AutoTrader
from .chat_engine import run_ai_chat
from .config import AppConfig, PROJECT_ROOT, public_config, save_runtime_risk_config
from .evaluation import build_evaluation_payload, collect_evaluation_codes, decision_evaluation_id
from .futu_client import FutuPaperClient, _load_futu
from .futu_sync import apply_order_with_optional_futu_sync, refresh_futu_sync_orders
from .market_data import auto_loop_session
from .models import OrderIntent
from .news_signals import load_news_signals
from .portfolios import (
    clone_portfolio,
    create_portfolio,
    delete_portfolio,
    delete_position,
    delete_user_trade,
    effective_fx_payload,
    get_portfolio,
    load_portfolios,
    set_active_portfolio,
    update_portfolio_cash,
    update_portfolio_fx_rates,
    update_portfolio_settings,
    upsert_position,
)
from .storage import atomic_write_text, file_lock
from .watchlist import add_user_watch, load_user_watchlist, load_watchlist, remove_user_watch


STATIC_ROOT = PROJECT_ROOT / "web"
DECISION_LOG_ROOT = PROJECT_ROOT / "data" / "decisions"
GEMINI_STANDARD_PRICES = {
    "gemini-3.5-flash": {"input_per_1m": 1.50, "output_per_1m": 9.00},
    "gemini-3-flash-preview": {"input_per_1m": 0.50, "output_per_1m": 3.00},
    "gemini-3.1-flash-lite": {"input_per_1m": 0.25, "output_per_1m": 1.50},
}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        return _jsonable(value.item())
    return str(value)


def _doctor_payload(config: AppConfig) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        futu = _load_futu(config.use_system_home)
        checks.append(
            {
                "name": "futu-api",
                "ok": True,
                "details": {
                    "ret_ok": futu.RET_OK,
                    "trd_env": futu.TrdEnv.SIMULATE,
                    "markets": [futu.TrdMarket.US, futu.TrdMarket.HK, futu.TrdMarket.CN],
                },
            }
        )
    except Exception as exc:
        checks.append({"name": "futu-api", "ok": False, "error": str(exc)})

    target = f"{config.opend_host}:{config.opend_port}"
    try:
        with socket.create_connection((config.opend_host, config.opend_port), timeout=2.0):
            pass
        checks.append({"name": "OpenD", "ok": True, "target": target})
    except OSError as exc:
        checks.append({"name": "OpenD", "ok": False, "target": target, "error": str(exc)})

    checks.append({"name": "Paper", "ok": True, "details": "TrdEnv.SIMULATE"})
    return {
        "ok": all(check["ok"] for check in checks),
        "config": public_config(config),
        "checks": checks,
        "market_schedule": auto_loop_session(config.gemini.observe_markets),
    }


def _read_decisions(limit: int) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    if not DECISION_LOG_ROOT.exists():
        return []

    entries: deque[dict[str, Any]] = deque(maxlen=limit)
    for log_path in sorted(DECISION_LOG_ROOT.glob("*.jsonl")):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entry.setdefault("log_date", log_path.stem)
                entry.setdefault("evaluation_id", decision_evaluation_id(entry))
                entries.append(entry)
    return list(reversed(entries))


def _read_decisions_page(
    *,
    page: int,
    page_size: int,
    action: str = "ALL",
    date_start: str = "",
    date_end: str = "",
    portfolio_id: str = "",
) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    action = action.upper()
    portfolio_id = str(portfolio_id or "").strip()
    entries: list[dict[str, Any]] = []

    if DECISION_LOG_ROOT.exists():
        for log_path in sorted(DECISION_LOG_ROOT.glob("*.jsonl")):
            log_date = log_path.stem
            if date_start and log_date < date_start:
                continue
            if date_end and log_date > date_end:
                continue
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                decision_action = str(entry.get("decision", {}).get("action", "")).upper()
                if action != "ALL" and decision_action != action:
                    continue
                if portfolio_id and portfolio_id != "ALL":
                    entry_portfolio = entry.get("portfolio") or {}
                    if str(entry_portfolio.get("id") or "") != portfolio_id:
                        continue
                entry.setdefault("log_date", log_date)
                entry.setdefault("evaluation_id", decision_evaluation_id(entry))
                entries.append(entry)

    entries.sort(key=lambda item: str(item.get("timestamp") or item.get("ts") or ""), reverse=True)
    total = len(entries)
    total_pages = max(1, math.ceil(total / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    return {
        "ok": True,
        "count": len(entries[start : start + page_size]),
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "entries": entries[start : start + page_size],
    }


def _read_decisions_filtered(
    *,
    limit: int = 500,
    date_start: str = "",
    date_end: str = "",
    portfolio_id: str = "",
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    portfolio_id = str(portfolio_id or "").strip()
    entries: list[dict[str, Any]] = []

    if DECISION_LOG_ROOT.exists():
        for log_path in sorted(DECISION_LOG_ROOT.glob("*.jsonl")):
            log_date = log_path.stem
            if date_start and log_date < date_start:
                continue
            if date_end and log_date > date_end:
                continue
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if portfolio_id and portfolio_id != "ALL":
                    entry_portfolio = entry.get("portfolio") or {}
                    if str(entry_portfolio.get("id") or "") != portfolio_id:
                        continue
                entry.setdefault("log_date", log_date)
                entry.setdefault("evaluation_id", decision_evaluation_id(entry))
                entries.append(entry)

    entries.sort(key=lambda item: str(item.get("timestamp") or item.get("ts") or ""), reverse=True)
    return entries[:limit]


def _read_decisions_for_date(date_text: str) -> list[dict[str, Any]]:
    log_path = DECISION_LOG_ROOT / f"{date_text}.jsonl"
    if not log_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entry.setdefault("evaluation_id", decision_evaluation_id(entry))
            entries.append(entry)
    return entries


def _find_decision(decision_id: str) -> dict[str, Any] | None:
    decision_id = str(decision_id or "").strip()
    if not decision_id or not DECISION_LOG_ROOT.exists():
        return None
    for log_path in sorted(DECISION_LOG_ROOT.glob("*.jsonl"), reverse=True):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and str(entry.get("decision_id") or "") == decision_id:
                entry.setdefault("log_date", log_path.stem)
                return entry
    return None


def _mark_decision_application(decision_id: str, application: dict[str, Any]) -> bool:
    decision_id = str(decision_id or "").strip()
    if not decision_id or not DECISION_LOG_ROOT.exists():
        return False
    for log_path in sorted(DECISION_LOG_ROOT.glob("*.jsonl"), reverse=True):
        with file_lock(log_path):
            changed = False
            next_lines: list[str] = []
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    next_lines.append(line)
                    continue
                if isinstance(entry, dict) and str(entry.get("decision_id") or "") == decision_id:
                    entry["application"] = _jsonable(application)
                    changed = True
                next_lines.append(json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else line)
            if changed:
                atomic_write_text(log_path, "\n".join(next_lines) + "\n", encoding="utf-8")
                return True
    return False


def _mark_decision_human_review(decision_ref: str, review: dict[str, Any]) -> dict[str, Any]:
    decision_ref = str(decision_ref or "").strip()
    if not decision_ref or not DECISION_LOG_ROOT.exists():
        raise ValueError("decision_id is required")
    label = str(review.get("label") or "").strip()
    allowed_labels = {"", "correct", "too_early", "too_late", "wrong", "risk_saved_loss"}
    if label not in allowed_labels:
        raise ValueError("invalid review label")
    human_review = {
        "label": label,
        "note": str(review.get("note") or "").strip()[:1000],
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    for log_path in sorted(DECISION_LOG_ROOT.glob("*.jsonl"), reverse=True):
        with file_lock(log_path):
            changed = False
            next_lines: list[str] = []
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    next_lines.append(line)
                    continue
                if not isinstance(entry, dict):
                    next_lines.append(line)
                    continue
                entry.setdefault("log_date", log_path.stem)
                evaluation_id = decision_evaluation_id(entry)
                if str(entry.get("decision_id") or "") == decision_ref or evaluation_id == decision_ref:
                    entry["human_review"] = human_review
                    changed = True
                next_lines.append(json.dumps(entry, ensure_ascii=False))
            if changed:
                atomic_write_text(log_path, "\n".join(next_lines) + "\n", encoding="utf-8")
                return human_review
    raise ValueError("decision not found")


def _num(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _estimate_missing_usage(entry: dict[str, Any]) -> dict[str, int]:
    prompt_payload = {
        "candidates": entry.get("candidates", []),
        "order": entry.get("order"),
        "blocked_reasons": entry.get("blocked_reasons", []),
    }
    response_payload = entry.get("decision", {})
    prompt_chars = len(json.dumps(prompt_payload, ensure_ascii=False, default=str))
    response_chars = len(json.dumps(response_payload, ensure_ascii=False, default=str))
    return {
        "prompt_token_count": max(900, round(prompt_chars / 2.0) + 900),
        "candidates_token_count": max(120, round(response_chars / 1.8)),
        "thoughts_token_count": 250,
    }


def _usage_cost(input_tokens: int, output_tokens: int, price: dict[str, float]) -> float:
    return (input_tokens / 1_000_000 * price["input_per_1m"]) + (
        output_tokens / 1_000_000 * price["output_per_1m"]
    )


def _gemini_usage_payload(config: AppConfig, date_text: str) -> dict[str, Any]:
    entries = _read_decisions_for_date(date_text)
    model = config.gemini.model
    price = GEMINI_STANDARD_PRICES.get(model, GEMINI_STANDARD_PRICES["gemini-3.5-flash"])
    totals = {
        "prompt_token_count": 0,
        "candidates_token_count": 0,
        "thoughts_token_count": 0,
        "tool_use_prompt_token_count": 0,
        "total_token_count": 0,
    }
    measured_calls = 0

    for entry in entries:
        usage = entry.get("gemini_usage") or {}
        estimated = False
        if not usage:
            usage = _estimate_missing_usage(entry)
            estimated = True
        else:
            measured_calls += 1

        prompt_tokens = _num(usage.get("prompt_token_count")) + _num(usage.get("tool_use_prompt_token_count"))
        output_tokens = _num(usage.get("candidates_token_count")) + _num(usage.get("thoughts_token_count"))
        totals["prompt_token_count"] += _num(usage.get("prompt_token_count"))
        totals["candidates_token_count"] += _num(usage.get("candidates_token_count"))
        totals["thoughts_token_count"] += _num(usage.get("thoughts_token_count"))
        totals["tool_use_prompt_token_count"] += _num(usage.get("tool_use_prompt_token_count"))
        if usage.get("total_token_count") and not estimated:
            totals["total_token_count"] += _num(usage.get("total_token_count"))
        else:
            totals["total_token_count"] += prompt_tokens + output_tokens

    input_tokens = totals["prompt_token_count"] + totals["tool_use_prompt_token_count"]
    output_tokens = totals["candidates_token_count"] + totals["thoughts_token_count"]
    per_call_cost = _usage_cost(input_tokens, output_tokens, price) / len(entries) if entries else 0.0
    cycles_per_day = max(1, round(86400 / max(config.gemini.loop_interval_seconds, 30)))
    projected_daily_cost = per_call_cost * cycles_per_day

    return {
        "ok": True,
        "date": date_text,
        "model": model,
        "calls": len(entries),
        "measured_calls": measured_calls,
        "estimated_calls": len(entries) - measured_calls,
        "loop_interval_seconds": config.gemini.loop_interval_seconds,
        "projected_calls_per_day": cycles_per_day,
        "tokens": totals,
        "price": price,
        "paid_estimate_usd": round(_usage_cost(input_tokens, output_tokens, price), 6),
        "projected_paid_usd_per_day": round(projected_daily_cost, 6),
        "free_tier_cost_usd": 0.0,
    }


class PaperWebHandler(BaseHTTPRequestHandler):
    server_version = "FutuPaperAI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self._serve_static(parsed.path)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        self._serve_static(parsed.path, head_only=True)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        self._handle_api_post(parsed.path)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    @property
    def config(self) -> AppConfig:
        return AppConfig.from_env()

    @property
    def client(self) -> FutuPaperClient:
        return FutuPaperClient(self.config)

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path == "/api/status":
                self._send_json(_doctor_payload(self.config))
            elif path == "/api/snapshot":
                codes = self._query_list(query, "codes")
                self._send_json(self.client.snapshot(codes))
            elif path == "/api/account":
                market = self._query_one(query, "market", "US").upper()
                currency = self._query_one(query, "currency", "USD").upper()
                self._send_json(self.client.account(market, currency))
            elif path == "/api/positions":
                market = self._query_one(query, "market", "US").upper()
                self._send_json(self.client.positions(market))
            elif path == "/api/fx-rates":
                self._send_json(self.client.fx_rates_to_hkd())
            elif path == "/api/config":
                self._send_json({"ok": True, "config": public_config(self.config)})
            elif path == "/api/watchlist":
                markets = set(self._query_list(query, "markets")) if query.get("markets") else None
                items = load_watchlist(markets=markets)
                self._send_json({"ok": True, "count": len(items), "items": [item.__dict__ for item in items]})
            elif path == "/api/my-watchlist":
                self._send_json(self._my_watchlist_payload())
            elif path == "/api/portfolios":
                self._send_json(self._portfolio_payload())
            elif path == "/api/decisions":
                page = self._query_int(query, "page", 1)
                page_size = self._query_int(query, "page_size", self._query_int(query, "limit", 20))
                action = self._query_one(query, "action", "ALL")
                date_start = self._query_one(query, "date_start", "")
                date_end = self._query_one(query, "date_end", "")
                portfolio_id = self._query_one(query, "portfolio_id", "")
                self._send_json(
                    _read_decisions_page(
                        page=page,
                        page_size=page_size,
                        action=action,
                        date_start=date_start,
                        date_end=date_end,
                        portfolio_id=portfolio_id,
                    )
                )
            elif path == "/api/evaluation":
                limit = self._query_int(query, "limit", 500)
                date_start = self._query_one(query, "date_start", "")
                date_end = self._query_one(query, "date_end", "")
                portfolio_id = self._query_one(query, "portfolio_id", "")
                store = load_portfolios()
                entries = _read_decisions_filtered(
                    limit=limit,
                    date_start=date_start,
                    date_end=date_end,
                    portfolio_id=portfolio_id,
                )
                codes = collect_evaluation_codes(store.get("portfolios", []), entries)
                quote_by_code, quote_error = self._quote_map(codes)
                fx_payload = self.client.fx_rates_to_hkd()
                self._send_json(
                    build_evaluation_payload(
                        portfolios=store.get("portfolios", []),
                        entries=entries,
                        quote_by_code=quote_by_code,
                        fx_payload=fx_payload,
                        quote_error=quote_error,
                        portfolio_id="" if portfolio_id == "ALL" else portfolio_id,
                    )
                )
            elif path == "/api/gemini-usage":
                date_text = self._query_one(query, "date", datetime.now().date().isoformat())
                self._send_json(_gemini_usage_payload(self.config, date_text))
            elif path == "/api/news-signals":
                display_limit = self._query_int(query, "limit", 50)
                min_impact = self._query_int(query, "min_impact", self.config.news.min_impact)
                lookback_hours = self._query_int(query, "lookback_hours", self.config.news.lookback_hours)
                news_config = replace(
                    self.config.news,
                    max_signals=max(1, min(display_limit, 200)),
                    min_impact=max(0, min(min_impact, 100)),
                    lookback_hours=max(1, min(lookback_hours, 168)),
                )
                self._send_json(load_news_signals(news_config))
            else:
                self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_api_post(self, path: str) -> None:
        try:
            payload = self._read_json()
            if path == "/api/validate":
                intent = OrderIntent.from_dict(payload)
                decision = self.client.validate(intent)
                self._send_json({"ok": decision.approved, "intent": intent.to_dict(), "violations": decision.violations})
            elif path == "/api/place":
                intent_payload = payload.get("intent", payload)
                execute = bool(payload.get("execute", False))
                intent = OrderIntent.from_dict(intent_payload)
                self._send_json(self.client.place_order(intent, execute=execute))
            elif path == "/api/risk-config":
                save_runtime_risk_config(payload.get("risk", payload))
                config = self.config
                self._send_json({"ok": True, "risk": public_config(config)["risk"], "config": public_config(config)})
            elif path == "/api/ai/once":
                execute = bool(payload.get("execute", False))
                notes = payload.get("notes") or []
                if not isinstance(notes, list):
                    raise ValueError("notes must be a list")
                result = AutoTrader(self.config).run_portfolios_once(execute=execute, notes=[str(note) for note in notes])
                self._send_json(result)
            elif path == "/api/ai/chat":
                messages = payload.get("messages") or []
                if not isinstance(messages, list):
                    raise ValueError("messages must be a list")
                result = run_ai_chat(
                    self.config,
                    topic=str(payload.get("topic", "")),
                    messages=messages,
                    use_news=bool(payload.get("use_news", True)),
                    use_web=bool(payload.get("use_web", False)),
                    portfolio_id=str(payload.get("portfolio_id", "")).strip() or None,
                )
                self._send_json(result)
            elif path == "/api/portfolios/create":
                store = create_portfolio(
                    str(payload.get("name", "")).strip(),
                    base_currency=str(payload.get("base_currency", "HKD")),
                    cash=_float(payload.get("cash", 0)),
                )
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/clone":
                store = clone_portfolio(
                    str(payload.get("portfolio_id", "")).strip() or None,
                    name=str(payload.get("name", "")).strip(),
                    apply_mode=str(payload.get("apply_mode", "")).strip() or None,
                )
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/delete":
                store = delete_portfolio(str(payload.get("portfolio_id", "")).strip())
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/active":
                store = set_active_portfolio(str(payload.get("portfolio_id", "")).strip())
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/settings":
                store = update_portfolio_settings(
                    str(payload.get("portfolio_id", "")).strip() or None,
                    apply_mode=str(payload.get("apply_mode", "")).strip() or None,
                    portfolio_kind=str(payload.get("portfolio_kind", "")).strip() or None,
                    futu_sync_enabled=bool(payload["futu_sync_enabled"]) if "futu_sync_enabled" in payload else None,
                    strategy_profile=str(payload.get("strategy_profile", "")).strip() or None,
                    strategy_tags=payload.get("strategy_tags") if "strategy_tags" in payload else None,
                    strategy_hypothesis=payload.get("strategy_hypothesis") if "strategy_hypothesis" in payload else None,
                    prompt_template=str(payload.get("prompt_template", "")) if "prompt_template" in payload else None,
                    risk_overrides=payload.get("risk_overrides") if "risk_overrides" in payload else None,
                    ai_loop_enabled=bool(payload["ai_loop_enabled"]) if "ai_loop_enabled" in payload else None,
                )
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/cash":
                store = update_portfolio_cash(
                    str(payload.get("portfolio_id", "")).strip() or None,
                    payload.get("cash", 0),
                    currency=str(payload.get("currency", "")).strip() or None,
                )
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/fx":
                store = update_portfolio_fx_rates(
                    str(payload.get("portfolio_id", "")).strip() or None,
                    payload.get("fx_to_hkd") if isinstance(payload.get("fx_to_hkd"), dict) else payload,
                )
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/position":
                store = upsert_position(str(payload.get("portfolio_id", "")).strip() or None, payload.get("position", payload))
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/position/delete":
                store = delete_position(
                    str(payload.get("portfolio_id", "")).strip() or None,
                    str(payload.get("code", "")).strip(),
                )
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/portfolios/trade":
                upstream_fx_payload = self.client.fx_rates_to_hkd()
                portfolio_id = str(payload.get("portfolio_id", "")).strip() or None
                portfolio = get_portfolio(portfolio_id)
                fx_payload = effective_fx_payload(portfolio, upstream_fx_payload)
                application = apply_order_with_optional_futu_sync(
                    client=self.client,
                    portfolio=portfolio,
                    portfolio_id=str(portfolio.get("id") or portfolio_id or ""),
                    order_payload=payload.get("order", payload),
                    source="user_trade",
                    decision_id="",
                    reason=str(payload.get("reason", "")).strip() or "用户在组合页提交/记录交易",
                    fx_to_hkd=fx_payload.get("fx_to_hkd"),
                    fx_source=str(fx_payload.get("source") or ""),
                    fx_status=fx_payload,
                )
                self._send_json({"ok": bool(application.get("ok", True)), "application": application, "portfolio_payload": self._portfolio_payload()})
            elif path == "/api/portfolios/trade/delete":
                store = delete_user_trade(
                    str(payload.get("portfolio_id", "")).strip() or None,
                    str(payload.get("trade_id", "")).strip(),
                )
                self._send_json(self._portfolio_payload(store))
            elif path == "/api/decisions/apply":
                decision_id = str(payload.get("decision_id", "")).strip()
                if not decision_id:
                    raise ValueError("decision_id is required")
                entry = _find_decision(decision_id) or {}
                existing_application = entry.get("application") if isinstance(entry.get("application"), dict) else {}
                if existing_application.get("status") in {
                    "applied",
                    "already_applied",
                    "futu_submitted",
                    "partially_applied",
                    "local_apply_failed",
                }:
                    self._send_json({"ok": True, "application": existing_application, "portfolio_payload": self._portfolio_payload()})
                    return
                order = payload.get("order") or entry.get("order")
                if not isinstance(order, dict):
                    raise ValueError("decision has no order to apply")
                portfolio_id = str(payload.get("portfolio_id", "")).strip()
                if not portfolio_id:
                    portfolio_id = str((entry.get("portfolio") or {}).get("id") or "").strip()
                if not portfolio_id:
                    raise ValueError("portfolio_id is required")
                decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
                upstream_fx_payload = self.client.fx_rates_to_hkd()
                portfolio = get_portfolio(portfolio_id)
                fx_payload = effective_fx_payload(portfolio, upstream_fx_payload)
                application = apply_order_with_optional_futu_sync(
                    client=self.client,
                    portfolio=portfolio,
                    portfolio_id=portfolio_id,
                    order_payload=order,
                    source="manual",
                    decision_id=decision_id,
                    reason=str(decision.get("reason") or order.get("reason") or ""),
                    fx_to_hkd=fx_payload.get("fx_to_hkd"),
                    fx_source=str(fx_payload.get("source") or ""),
                    fx_status=fx_payload,
                )
                _mark_decision_application(decision_id, application)
                self._send_json({"ok": bool(application.get("ok", True)), "application": application, "portfolio_payload": self._portfolio_payload()})
            elif path == "/api/decisions/review":
                decision_ref = str(payload.get("decision_id") or payload.get("evaluation_id") or "").strip()
                human_review = _mark_decision_human_review(decision_ref, payload.get("review", payload))
                self._send_json({"ok": True, "decision_id": decision_ref, "human_review": human_review})
            elif path == "/api/my-watchlist/add":
                code = str(payload.get("code", "")).strip().upper()
                if not code:
                    raise ValueError("code is required")
                name = str(payload.get("name", "")).strip()
                sector = str(payload.get("sector", "Other")).strip() or "Other"
                add_user_watch(code, name=name, sector=sector)
                self._send_json(self._my_watchlist_payload())
            elif path == "/api/my-watchlist/remove":
                code = str(payload.get("code", "")).strip().upper()
                if not code:
                    raise ValueError("code is required")
                remove_user_watch(code)
                self._send_json(self._my_watchlist_payload())
            else:
                self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _my_watchlist_payload(self) -> dict[str, Any]:
        items = load_user_watchlist()
        codes = [item.code for item in items]
        quote_rows: list[dict[str, Any]] = []
        quote_error = ""
        if codes:
            quote_payload = self.client.snapshot(codes)
            if quote_payload.get("ok"):
                quote_by_code = {str(row.get("code", "")).upper(): row for row in quote_payload.get("data") or []}
                for item in items:
                    row = dict(quote_by_code.get(item.code, {}))
                    row.setdefault("code", item.code)
                    row["watch_name"] = item.name
                    row["watch_sector"] = item.sector
                    row["watch_tier"] = item.tier
                    row["watch_role"] = item.role
                    row["market"] = item.market
                    quote_rows.append(row)
            else:
                quote_error = str(quote_payload.get("data") or quote_payload.get("error") or "quote request failed")

        return {
            "ok": not quote_error,
            "count": len(items),
            "items": [item.__dict__ for item in items],
            "quotes": quote_rows,
            "quote_error": quote_error,
        }

    def _quote_map(self, codes: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
        normalized_codes = sorted({str(code or "").strip().upper() for code in codes if str(code or "").strip()})
        if not normalized_codes:
            return {}, ""
        try:
            with socket.create_connection((self.config.opend_host, self.config.opend_port), timeout=1.2):
                pass
        except OSError as exc:
            return {}, f"OpenD unavailable: {exc}"

        payload = self.client.snapshot(normalized_codes)
        if not payload.get("ok"):
            return {}, str(payload.get("data") or payload.get("error") or "quote request failed")
        return {str(row.get("code", "")).upper(): row for row in payload.get("data") or [] if isinstance(row, dict)}, ""

    def _portfolio_payload(self, store: dict[str, Any] | None = None) -> dict[str, Any]:
        store = store or load_portfolios()
        upstream_fx_payload = self.client.fx_rates_to_hkd()
        futu_sync_results: list[dict[str, Any]] = []
        for portfolio in store.get("portfolios", []):
            if not portfolio.get("futu_sync_enabled"):
                continue
            if not any(
                str(order.get("status") or "").lower()
                in {"submitted", "futu_submitted", "partial", "partially_applied", "local_apply_failed"}
                for order in portfolio.get("futu_sync_orders", [])
                if isinstance(order, dict)
            ):
                continue
            portfolio_fx_payload = effective_fx_payload(portfolio, upstream_fx_payload)
            futu_sync_results.extend(
                refresh_futu_sync_orders(
                    client=self.client,
                    portfolio=portfolio,
                    fx_to_hkd=portfolio_fx_payload.get("fx_to_hkd"),
                    fx_source=str(portfolio_fx_payload.get("source") or ""),
                    fx_status=portfolio_fx_payload,
                )
            )
        for sync_result in futu_sync_results:
            if not isinstance(sync_result, dict):
                continue
            decision_id = str(sync_result.get("decision_id") or "").strip()
            application = sync_result.get("application")
            if decision_id and isinstance(application, dict):
                _mark_decision_application(decision_id, application)
        if futu_sync_results:
            store = load_portfolios()
        codes = sorted(
            {
                str(position.get("code", "")).upper()
                for portfolio in store.get("portfolios", [])
                for position in portfolio.get("positions", [])
                if position.get("code")
            }
        )
        quote_by_code, quote_error = self._quote_map(codes)
        portfolios: list[dict[str, Any]] = []
        for portfolio in store.get("portfolios", []):
            fx_payload = effective_fx_payload(portfolio, upstream_fx_payload)
            portfolio_row = dict(portfolio)
            portfolio_row["fx_to_hkd"] = fx_payload.get("fx_to_hkd") or portfolio.get("fx_to_hkd", {})
            portfolio_row["fx_source"] = fx_payload.get("source")
            portfolio_row["fx_ok"] = bool(fx_payload.get("ok"))
            portfolio_row["fx_error"] = fx_payload.get("error")
            portfolio_row["fx_updated_at"] = fx_payload.get("updated_at")
            portfolio_row["futu_sync_pending_count"] = sum(
                1
                for order in portfolio.get("futu_sync_orders", [])
                if isinstance(order, dict)
                and str(order.get("status") or "").lower()
                in {"submitted", "futu_submitted", "partial", "partially_applied", "local_apply_failed"}
            )
            enriched_positions: list[dict[str, Any]] = []
            totals: dict[str, dict[str, float]] = {}
            for position in portfolio.get("positions", []):
                row = dict(position)
                quote = dict(quote_by_code.get(str(row.get("code", "")).upper(), {}))
                qty = _float(row.get("qty"))
                cost_price = _float(row.get("cost_price"))
                last_price = _float(quote.get("last_price") or quote.get("nominal_price") or quote.get("bid_price"))
                currency = str(row.get("currency") or portfolio.get("base_currency") or "USD").upper()
                cost_value = qty * cost_price
                market_value = qty * last_price if last_price > 0 else 0.0
                pl_value = market_value - cost_value if last_price > 0 else 0.0
                row["quote"] = quote
                row["last_price"] = last_price if last_price > 0 else None
                row["price_source"] = "Futu OpenD snapshot" if last_price > 0 else ""
                row["extended_session"] = quote.get("extended_session") or {}
                row["cost_value"] = round(cost_value, 4)
                row["market_value"] = round(market_value, 4) if last_price > 0 else None
                row["pl_value"] = round(pl_value, 4) if last_price > 0 else None
                row["pl_ratio"] = round(pl_value / cost_value * 100, 4) if last_price > 0 and cost_value > 0 else None
                enriched_positions.append(row)

                bucket = totals.setdefault(currency, {"cost_value": 0.0, "market_value": 0.0, "pl_value": 0.0})
                bucket["cost_value"] += cost_value
                if last_price > 0:
                    bucket["market_value"] += market_value
                    bucket["pl_value"] += pl_value

            portfolio_row["positions"] = enriched_positions
            portfolio_row["position_count"] = len(enriched_positions)
            portfolio_row["totals_by_currency"] = {
                currency: {key: round(value, 4) for key, value in values.items()}
                for currency, values in totals.items()
            }
            portfolios.append(portfolio_row)

        return {
            "ok": not quote_error,
            "active_id": store.get("active_id"),
            "portfolios": portfolios,
            "quote_error": quote_error,
            "fx": upstream_fx_payload,
            "futu_sync_results": futu_sync_results,
        }

    def _serve_static(self, path: str, head_only: bool = False) -> None:
        target = "index.html" if path in {"", "/"} else path.lstrip("/")
        file_path = (STATIC_ROOT / target).resolve()
        if STATIC_ROOT.resolve() not in file_path.parents and file_path != STATIC_ROOT.resolve():
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if not file_path.exists() or not file_path.is_file():
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _query_one(self, query: dict[str, list[str]], key: str, default: str) -> str:
        values = query.get(key)
        if not values:
            return default
        return values[0]

    def _query_list(self, query: dict[str, list[str]], key: str) -> list[str]:
        raw_values = query.get(key, [])
        codes: list[str] = []
        for raw in raw_values:
            codes.extend(item.strip().upper() for item in raw.split(",") if item.strip())
        if not codes:
            raise ValueError(f"Missing query parameter: {key}")
        return codes

    def _query_int(self, query: dict[str, list[str]], key: str, default: int) -> int:
        values = query.get(key)
        if not values:
            return default
        try:
            return int(values[0])
        except ValueError as exc:
            raise ValueError(f"Invalid integer query parameter: {key}") from exc

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(_jsonable(payload), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_web_server(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), PaperWebHandler)
    print(f"Futu paper AI web console: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping web console")
    finally:
        server.server_close()
