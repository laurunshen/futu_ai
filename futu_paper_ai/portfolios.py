from __future__ import annotations

import json
import uuid
from datetime import datetime
from math import ceil
from typing import Any

from .config import AppConfig, STATE_ROOT
from .market_data import market_session_payload
from .models import OrderIntent, infer_market
from .news_signals import normalize_ticker
from .risk import RiskEngine, risk_config_with_overrides
from .storage import atomic_write_text, file_lock


PORTFOLIOS_PATH = STATE_ROOT / "portfolios.json"
DEFAULT_PORTFOLIO_ID = "default"
APPLY_MODES = {"observe", "manual", "auto"}
PORTFOLIO_KINDS = {"paper", "actual"}
DEFAULT_FX_TO_HKD = {"HKD": 1.0, "USD": 7.8, "CNY": 1.08, "CNH": 1.08}
LOCAL_DEFAULT_FX_SOURCE = "local_default_fx_to_hkd"
BROKER_MANUAL_FX_SOURCE = "broker_manual_fx_to_hkd"
THIRD_PARTY_FX_SOURCE = "third_party_fx_to_hkd"
STRATEGY_PROFILES = {"general", "short_swing", "long_hold", "growth_aggressive", "defensive_cashflow", "news_driven"}
LOCAL_FEE_MODEL = "usmart_standard_commission_2026_screenshot_v1"
RISK_OVERRIDE_MARKETS = {"US", "HK", "CN"}
RISK_OVERRIDE_FLOAT_MAPS = {"max_order_value", "max_qty"}
RISK_OVERRIDE_FLOATS = {"max_position_pct", "max_equity_exposure_pct", "min_cash_pct"}
RISK_OVERRIDE_INTS = {"max_trades_per_day", "cooldown_minutes"}
RISK_OVERRIDE_BOOLS = {"require_whitelist", "allow_sell", "allow_market_orders"}
RISK_OVERRIDE_SETS = {"allowed_markets", "allowed_codes"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except (TypeError, ValueError):
        return default


def _currency_for_market(market: str) -> str:
    return {"US": "USD", "HK": "HKD", "CN": "CNY"}.get(market.upper(), "USD")


def _fx_rates_to_hkd(payload: dict[str, Any] | None = None) -> dict[str, float]:
    rates = dict(DEFAULT_FX_TO_HKD)
    raw = (payload or {}).get("fx_to_hkd")
    if isinstance(raw, dict):
        for currency, value in raw.items():
            code = str(currency or "").strip().upper()
            rate = _num(value, 0)
            if code and rate > 0:
                rates[code] = rate
    return rates


def _normalized_fx_rates(raw: dict[str, Any] | None) -> dict[str, float]:
    rates: dict[str, float] = {"HKD": 1.0}
    if isinstance(raw, dict):
        for currency, value in raw.items():
            code = str(currency or "").strip().upper()
            if not code:
                continue
            if code == "HKD":
                rates["HKD"] = 1.0
                continue
            rate = _num(value, 0)
            if rate > 0:
                rates[code] = round(rate, 6)
    return rates


def effective_fx_payload(portfolio: dict[str, Any], upstream_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    upstream_payload = dict(upstream_payload or {})
    stored_source = str(portfolio.get("fx_source") or "").strip()
    stored_rates = _normalized_fx_rates(portfolio.get("fx_to_hkd") if isinstance(portfolio, dict) else {})

    if stored_source == BROKER_MANUAL_FX_SOURCE and len(stored_rates) > 1:
        rates = dict(DEFAULT_FX_TO_HKD)
        rates.update(stored_rates)
        return {
            "ok": True,
            "source": BROKER_MANUAL_FX_SOURCE,
            "fx_to_hkd": {currency: round(value, 6) for currency, value in sorted(rates.items())},
            "error": "",
            "updated_at": str(portfolio.get("fx_updated_at") or portfolio.get("updated_at") or _now()),
            "upstream": upstream_payload,
        }

    if upstream_payload.get("ok") and isinstance(upstream_payload.get("fx_to_hkd"), dict):
        rates = dict(DEFAULT_FX_TO_HKD)
        rates.update(_normalized_fx_rates(upstream_payload.get("fx_to_hkd")))
        return {
            **upstream_payload,
            "ok": True,
            "source": str(upstream_payload.get("source") or "futu_opend_fx_snapshot"),
            "fx_to_hkd": {currency: round(value, 6) for currency, value in sorted(rates.items())},
        }

    rates = dict(DEFAULT_FX_TO_HKD)
    source = LOCAL_DEFAULT_FX_SOURCE
    if stored_source and stored_source != LOCAL_DEFAULT_FX_SOURCE and len(stored_rates) > 1:
        rates.update(stored_rates)
        source = stored_source
    return {
        "ok": source != LOCAL_DEFAULT_FX_SOURCE,
        "source": source,
        "fx_to_hkd": {currency: round(value, 6) for currency, value in sorted(rates.items())},
        "error": str(upstream_payload.get("error") or ""),
        "updated_at": str(portfolio.get("fx_updated_at") or portfolio.get("updated_at") or _now()),
        "upstream": upstream_payload,
    }


def _convert_currency(amount: float, from_currency: str, to_currency: str, rates: dict[str, float]) -> float:
    source = str(from_currency or "").upper()
    target = str(to_currency or "").upper()
    if source == target:
        return amount
    source_to_hkd = _num(rates.get(source), 0)
    target_to_hkd = _num(rates.get(target), 0)
    if source_to_hkd <= 0 or target_to_hkd <= 0:
        raise ValueError(f"missing FX rate for {source}->{target}")
    return amount * source_to_hkd / target_to_hkd


def _fee_line(name: str, amount: float, currency: str, rule: str) -> dict[str, Any]:
    return {
        "name": name,
        "amount": round(max(0.0, amount), 4),
        "currency": str(currency or "").upper(),
        "rule": rule,
    }


def _pct_fee(notional: float, pct: float, *, minimum: float = 0.0) -> float:
    if notional <= 0:
        return 0.0
    return max(notional * pct / 100, minimum)


def _per_share_fee(qty: float, rate: float, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    if qty <= 0:
        return 0.0
    amount = qty * rate
    if maximum is not None:
        amount = min(amount, maximum)
    return max(amount, minimum)


def _trade_fee(market: str, side: str, qty: float, notional: float) -> dict[str, Any]:
    market_key = str(market or "").upper()
    side_key = str(side or "").upper()
    lines: list[dict[str, Any]] = []

    if market_key == "HK":
        currency = "HKD"
        lines.extend(
            [
                _fee_line("佣金", _pct_fee(notional, 0.03), currency, "交易金额 * 0.03%，最低 0 HKD/笔"),
                _fee_line("平台使用费", 12.0 if notional > 0 else 0.0, currency, "12 HKD/笔"),
                _fee_line("交易费", _pct_fee(notional, 0.00565, minimum=0.01), currency, "交易金额 * 0.00565%，最低 0.01 HKD/笔"),
                _fee_line("结算交收费", _pct_fee(notional, 0.0042), currency, "交易金额 * 0.0042%"),
                _fee_line("证监会交易征费", _pct_fee(notional, 0.0027), currency, "交易金额 * 0.0027%"),
                _fee_line("会财局交易征费", _pct_fee(notional, 0.00015), currency, "交易金额 * 0.00015%"),
                _fee_line("股票印花税", ceil(notional * 0.001) if notional > 0 else 0.0, currency, "交易金额 * 0.1%，不足 1 HKD 作 1 HKD；按整港元向上取整"),
            ]
        )
    elif market_key == "US":
        currency = "USD"
        if qty > 0 and qty < 1:
            platform_fee = 0.99
            platform_rule = "碎股 < 1 股，平台费 0.99 USD/笔"
        else:
            per_share_platform = qty * 0.009
            capped_platform = min(per_share_platform, notional * 0.01) if notional > 0 else 0.0
            platform_fee = max(capped_platform, 1.88) if qty > 0 else 0.0
            platform_rule = "0.009 USD/股，最低 1.88 USD/笔，最高交易金额 1%；最低收费优先"
        lines.extend(
            [
                _fee_line("佣金", 0.0, currency, "0 USD/股，最低 0 USD/笔"),
                _fee_line("平台使用费", platform_fee, currency, platform_rule),
                _fee_line("交收费", _per_share_fee(qty, 0.003, minimum=0.01, maximum=notional * 0.03 if notional > 0 else None), currency, "0.003 USD/股，最低 0.01 USD，最高不超过交易金额 3%"),
                _fee_line("综合审计追踪费", _per_share_fee(qty, 0.000003, minimum=0.01), currency, "0.000003 USD/股，每笔最低 0.01 USD"),
            ]
        )
        if side_key == "SELL":
            lines.extend(
                [
                    _fee_line("证监会规费", _pct_fee(notional, 0.00206, minimum=0.01), currency, "仅卖出收取，交易金额 * 0.00206%，最低 0.01 USD"),
                    _fee_line("交易活动费", _per_share_fee(qty, 0.000195, minimum=0.01, maximum=9.79), currency, "仅卖出收取，0.000195 USD/股，最低 0.01 USD，最高 9.79 USD"),
                ]
            )
    elif market_key == "CN":
        currency = "CNY"
        lines.extend(
            [
                _fee_line("佣金", _pct_fee(notional, 0.02, minimum=5.0), currency, "交易金额 * 0.02%，最低 5 CNY/笔"),
                _fee_line("平台使用费", 12.0 if notional > 0 else 0.0, currency, "12 CNY/笔"),
                _fee_line("经手费", _pct_fee(notional, 0.00341, minimum=0.01), currency, "成交金额 * 0.00341%，最低 0.01 CNY/笔"),
                _fee_line("证管费", _pct_fee(notional, 0.002, minimum=0.01), currency, "成交金额 * 0.002%，最低 0.01 CNY/笔"),
                _fee_line("过户费", _pct_fee(notional, 0.001, minimum=0.01), currency, "成交金额 * 0.001%，最低 0.01 CNY/笔"),
                _fee_line("登记过户费", _pct_fee(notional, 0.002, minimum=0.01), currency, "成交金额 * 0.002%，最低 0.01 CNY/笔"),
            ]
        )
        if side_key == "SELL":
            lines.append(_fee_line("交易印花税", _pct_fee(notional, 0.05), currency, "仅卖出收取，成交金额 * 0.05%"))
    else:
        currency = _currency_for_market(market_key)
        lines.append(_fee_line("估算费用", _pct_fee(notional, 0.05), currency, "未知市场兜底：交易金额 * 0.05%"))

    lines = [line for line in lines if _num(line.get("amount"), 0) > 0]
    total = round(sum(_num(line.get("amount"), 0) for line in lines), 4)
    return {
        "total": total,
        "currency": currency,
        "bps": round(total / notional * 10000, 4) if notional > 0 else 0.0,
        "model": LOCAL_FEE_MODEL,
        "details": lines,
    }


def _cash_effect(currency: str, amount: float, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "currency": str(currency or "").upper(),
        "amount": round(amount, 4),
        "reason": reason,
        **{key: value for key, value in extra.items() if value not in {None, ""}},
    }


def _normalize_apply_mode(value: Any) -> str:
    mode = str(value or "manual").strip().lower()
    return mode if mode in APPLY_MODES else "manual"


def _normalize_portfolio_kind(value: Any) -> str:
    kind = str(value or "paper").strip().lower()
    if kind in {"actual", "real", "live", "mirror"}:
        return "actual"
    return kind if kind in PORTFOLIO_KINDS else "paper"


def _normalize_strategy_profile(value: Any) -> str:
    profile = str(value or "general").strip().lower()
    return profile if profile in STRATEGY_PROFILES else "general"


def _normalize_strategy_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        tag = str(item or "").strip()
        if not tag:
            continue
        tag = tag[:24]
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= 8:
            break
    return tags


def _normalize_string_set(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    items = {str(item or "").strip().upper() for item in raw_items if str(item or "").strip()}
    return sorted(items)


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_risk_overrides(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    overrides: dict[str, Any] = {}
    for key in RISK_OVERRIDE_SETS:
        if key not in value:
            continue
        items = _normalize_string_set(value.get(key))
        if items:
            overrides[key] = items
    for key in RISK_OVERRIDE_BOOLS:
        if key in value:
            overrides[key] = _bool_value(value.get(key))
    for key in RISK_OVERRIDE_FLOAT_MAPS:
        raw = value.get(key)
        if not isinstance(raw, dict):
            continue
        parsed: dict[str, float] = {}
        for market, amount in raw.items():
            market_key = str(market or "").strip().upper()
            number = _num(amount, -1)
            if market_key in RISK_OVERRIDE_MARKETS and number >= 0:
                parsed[market_key] = round(number, 4)
        if parsed:
            overrides[key] = dict(sorted(parsed.items()))
    for key in RISK_OVERRIDE_FLOATS:
        if key not in value:
            continue
        number = _num(value.get(key), -1)
        if number >= 0:
            overrides[key] = round(number, 4)
    for key in RISK_OVERRIDE_INTS:
        if key not in value:
            continue
        number = int(_num(value.get(key), -1))
        if number >= 0:
            overrides[key] = number
    return overrides


def _clip_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _normalize_strategy_hypothesis(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        text = _clip_text(value, 1000)
        return {"hypothesis": text} if text else {}
    if not isinstance(value, dict):
        return {}
    fields = {
        "version": 80,
        "benchmark": 40,
        "hypothesis": 1000,
        "expected_regime": 500,
        "success_metric": 500,
        "start_date": 40,
        "review_after": 80,
    }
    normalized: dict[str, str] = {}
    for key, limit in fields.items():
        text = _clip_text(value.get(key), limit)
        if text:
            normalized[key] = text
    return normalized


def _normalize_prompt_template(value: Any) -> str:
    return _clip_text(value, 1500)


def _portfolio_kind_label(kind: Any) -> str:
    return "实际仓位镜像" if _normalize_portfolio_kind(kind) == "actual" else "模拟实验盘"


def _normalize_cash_by_currency(payload: dict[str, Any], base_currency: str) -> dict[str, float]:
    raw = payload.get("cash_by_currency")
    cash_by_currency: dict[str, float] = {}
    if isinstance(raw, dict):
        for currency, value in raw.items():
            code = str(currency or "").strip().upper()
            if code:
                cash_by_currency[code] = max(0.0, _num(value, 0))
    cash_by_currency.setdefault(base_currency, max(0.0, _num(payload.get("cash", 0))))
    return cash_by_currency


def _normalize_trade(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(payload.get("id") or _new_id()),
        "decision_id": str(payload.get("decision_id") or ""),
        "source": str(payload.get("source") or "manual"),
        "side": str(payload.get("side") or "").upper(),
        "code": str(payload.get("code") or "").upper(),
        "qty": _num(payload.get("qty"), 0),
        "price": _num(payload.get("price"), 0),
        "currency": str(payload.get("currency") or "").upper(),
        "notional": round(_num(payload.get("notional"), 0), 4),
        "fees": round(_num(payload.get("fees"), 0), 4),
        "fee_currency": str(payload.get("fee_currency") or payload.get("currency") or "").upper(),
        "fee_bps": round(_num(payload.get("fee_bps"), 0), 4),
        "fee_model": str(payload.get("fee_model") or ""),
        "fee_details": [
            {
                "name": str(item.get("name") or ""),
                "amount": round(_num(item.get("amount"), 0), 4),
                "currency": str(item.get("currency") or payload.get("currency") or "").upper(),
                "rule": str(item.get("rule") or ""),
            }
            for item in payload.get("fee_details", [])
            if isinstance(item, dict)
        ],
        "net_cash_amount": round(_num(payload.get("net_cash_amount"), 0), 4),
        "realized_pnl": round(_num(payload.get("realized_pnl"), 0), 4),
        "cash_effects": list(payload.get("cash_effects") or []),
        "fx": dict(payload.get("fx") or {}),
        "reason": str(payload.get("reason") or ""),
        "created_at": str(payload.get("created_at") or _now()),
    }


def _normalize_sync_order(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(payload.get("id") or payload.get("order_id") or _new_id()),
        "decision_id": str(payload.get("decision_id") or ""),
        "source": str(payload.get("source") or "futu_sync"),
        "order_id": str(payload.get("order_id") or ""),
        "code": str(payload.get("code") or "").upper(),
        "side": str(payload.get("side") or "").upper(),
        "qty": _num(payload.get("qty"), 0),
        "price": _num(payload.get("price"), 0),
        "dealt_qty": _num(payload.get("dealt_qty"), 0),
        "dealt_avg_price": _num(payload.get("dealt_avg_price"), 0),
        "applied_qty": _num(payload.get("applied_qty"), 0),
        "status": str(payload.get("status") or "submitted"),
        "message": str(payload.get("message") or ""),
        "futu_order": dict(payload.get("futu_order") or {}),
        "futu_deals": list(payload.get("futu_deals") or []),
        "order_payload": dict(payload.get("order_payload") or {}),
        "created_at": str(payload.get("created_at") or _now()),
        "updated_at": str(payload.get("updated_at") or _now()),
    }


def _normalize_operation(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(payload.get("id") or _new_id()),
        "type": str(payload.get("type") or "note"),
        "source": str(payload.get("source") or ""),
        "title": str(payload.get("title") or ""),
        "summary": str(payload.get("summary") or ""),
        "code": str(payload.get("code") or "").upper(),
        "side": str(payload.get("side") or "").upper(),
        "qty": _num(payload.get("qty"), 0),
        "price": _num(payload.get("price"), 0),
        "currency": str(payload.get("currency") or "").upper(),
        "notional": round(_num(payload.get("notional"), 0), 4),
        "decision_id": str(payload.get("decision_id") or ""),
        "trade_id": str(payload.get("trade_id") or ""),
        "payload": dict(payload.get("payload") or {}),
        "created_at": str(payload.get("created_at") or _now()),
    }


def _operation_title(source: str, side: str = "") -> str:
    source_key = str(source or "").lower()
    if source_key == "auto":
        return "AI 自动应用"
    if source_key == "manual":
        return "AI 手动应用"
    if source_key == "futu_sync":
        return "富途成交回写"
    if source_key == "user_trade":
        return "本人交易记录"
    return f"{side} 交易".strip() or "交易记录"


def _operation_from_trade(trade: dict[str, Any]) -> dict[str, Any]:
    source = str(trade.get("source") or "")
    side = str(trade.get("side") or "").upper()
    code = str(trade.get("code") or "").upper()
    qty = _num(trade.get("qty"), 0)
    price = _num(trade.get("price"), 0)
    currency = str(trade.get("currency") or "").upper()
    return _normalize_operation(
        {
            "type": "trade",
            "source": source,
            "title": _operation_title(source, side),
            "summary": f"{side} {qty:g} {code} @ {price:g} {currency}".strip(),
            "code": code,
            "side": side,
            "qty": qty,
            "price": price,
            "currency": currency,
            "notional": trade.get("notional"),
            "decision_id": trade.get("decision_id"),
            "trade_id": trade.get("id"),
            "payload": {
                "reason": trade.get("reason"),
                "cash_effects": trade.get("cash_effects"),
                "fx": trade.get("fx"),
                "fees": trade.get("fees"),
                "fee_model": trade.get("fee_model"),
                "fee_details": trade.get("fee_details"),
                "net_cash_amount": trade.get("net_cash_amount"),
            },
            "created_at": trade.get("created_at"),
        }
    )


def _append_operation(portfolio: dict[str, Any], operation: dict[str, Any]) -> None:
    operations = [
        _normalize_operation(item)
        for item in portfolio.get("operations", [])
        if isinstance(item, dict)
    ]
    operations.append(_normalize_operation(operation))
    portfolio["operations"] = operations[-800:]


def _default_store() -> dict[str, Any]:
    now = _now()
    return {
        "active_id": DEFAULT_PORTFOLIO_ID,
        "portfolios": [
            {
                "id": DEFAULT_PORTFOLIO_ID,
                "name": "我的模拟盘",
                "base_currency": "HKD",
                "cash": 0.0,
                "cash_by_currency": {"HKD": 0.0},
                "fx_to_hkd": dict(DEFAULT_FX_TO_HKD),
                "apply_mode": "manual",
                "portfolio_kind": "paper",
                "strategy_profile": "general",
                "strategy_tags": [],
                "strategy_hypothesis": {},
                "prompt_template": "",
                "risk_overrides": {},
                "ai_loop_enabled": True,
                "futu_sync_enabled": False,
                "positions": [],
                "trades": [],
                "operations": [],
                "futu_sync_orders": [],
                "created_at": now,
                "updated_at": now,
            }
        ],
    }


def _normalize_code(value: Any) -> str:
    code = normalize_ticker(str(value or ""))
    if not code:
        raise ValueError("code must look like US.PDD, HK.09988, or 9988.HK")
    infer_market(code)
    return code


def _normalize_position(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    code = _normalize_code(payload.get("code", existing.get("code", "")))
    market = infer_market(code)
    qty = _num(payload.get("qty", existing.get("qty", 0)))
    cost_price = _num(payload.get("cost_price", existing.get("cost_price", 0)))
    if qty <= 0:
        raise ValueError("qty must be greater than 0")
    if cost_price <= 0:
        raise ValueError("cost_price must be greater than 0")

    now = _now()
    return {
        "id": code,
        "code": code,
        "market": market,
        "name": str(payload.get("name", existing.get("name", ""))).strip(),
        "qty": qty,
        "cost_price": cost_price,
        "currency": str(payload.get("currency", existing.get("currency", _currency_for_market(market)))).strip().upper(),
        "note": str(payload.get("note", existing.get("note", ""))).strip(),
        "created_at": str(existing.get("created_at") or now),
        "updated_at": now,
    }


def _normalize_portfolio(payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    portfolio_id = str(payload.get("id") or _new_id()).strip() or _new_id()
    name = str(payload.get("name") or "未命名模拟盘").strip()[:40] or "未命名模拟盘"
    base_currency = str(payload.get("base_currency") or "HKD").strip().upper()
    cash_by_currency = _normalize_cash_by_currency(payload, base_currency)
    positions = []
    for raw_position in payload.get("positions") or []:
        if isinstance(raw_position, dict):
            positions.append(_normalize_position(raw_position))
    trades = [
        _normalize_trade(item)
        for item in payload.get("trades", [])
        if isinstance(item, dict)
    ][-500:]
    sync_orders = [
        _normalize_sync_order(item)
        for item in payload.get("futu_sync_orders", [])
        if isinstance(item, dict)
    ][-500:]
    operations = [
        _normalize_operation(item)
        for item in payload.get("operations", [])
        if isinstance(item, dict)
    ][-800:]
    portfolio_kind = _normalize_portfolio_kind(payload.get("portfolio_kind") or payload.get("portfolio_type"))
    return {
        "id": portfolio_id,
        "name": name,
        "portfolio_kind": portfolio_kind,
        "portfolio_kind_label": _portfolio_kind_label(portfolio_kind),
        "base_currency": base_currency,
        "cash": round(cash_by_currency.get(base_currency, 0.0), 4),
        "cash_by_currency": {currency: round(value, 4) for currency, value in sorted(cash_by_currency.items())},
        "fx_to_hkd": {currency: round(value, 6) for currency, value in sorted(_fx_rates_to_hkd(payload).items())},
        "fx_source": str(payload.get("fx_source") or LOCAL_DEFAULT_FX_SOURCE),
        "fx_status": dict(payload.get("fx_status") or {}),
        "fx_updated_at": str(payload.get("fx_updated_at") or ""),
        "apply_mode": _normalize_apply_mode(payload.get("apply_mode")),
        "strategy_profile": _normalize_strategy_profile(payload.get("strategy_profile")),
        "strategy_tags": _normalize_strategy_tags(payload.get("strategy_tags")),
        "strategy_hypothesis": _normalize_strategy_hypothesis(payload.get("strategy_hypothesis")),
        "prompt_template": _normalize_prompt_template(payload.get("prompt_template")),
        "risk_overrides": _normalize_risk_overrides(payload.get("risk_overrides")),
        "ai_loop_enabled": bool(payload.get("ai_loop_enabled", True)),
        "futu_sync_enabled": bool(payload.get("futu_sync_enabled", False)),
        "parent_id": str(payload.get("parent_id") or ""),
        "positions": positions,
        "trades": trades,
        "operations": operations,
        "futu_sync_orders": sync_orders,
        "created_at": str(payload.get("created_at") or now),
        "updated_at": str(payload.get("updated_at") or now),
    }


def _normalize_store(payload: dict[str, Any]) -> dict[str, Any]:
    portfolios = [
        _normalize_portfolio(item)
        for item in payload.get("portfolios", [])
        if isinstance(item, dict)
    ]
    if not portfolios:
        return _default_store()

    active_id = str(payload.get("active_id") or portfolios[0]["id"])
    portfolio_ids = {item["id"] for item in portfolios}
    if active_id not in portfolio_ids:
        active_id = portfolios[0]["id"]
    return {"active_id": active_id, "portfolios": portfolios}


def _load_store_unlocked() -> dict[str, Any]:
    if not PORTFOLIOS_PATH.exists():
        return _default_store()
    try:
        payload = json.loads(PORTFOLIOS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return _normalize_store(payload if isinstance(payload, dict) else {})


def _save_portfolios_unlocked(store: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_store(store)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_write_text(PORTFOLIOS_PATH, json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def _load_store() -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH, exclusive=False):
        return _load_store_unlocked()


def save_portfolios(store: dict[str, Any]) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        return _save_portfolios_unlocked(store)


def load_portfolios() -> dict[str, Any]:
    return _load_store()


def get_portfolio(portfolio_id: str | None = None) -> dict[str, Any]:
    store = load_portfolios()
    target_id = str(portfolio_id or store["active_id"])
    for portfolio in store["portfolios"]:
        if portfolio["id"] == target_id:
            return portfolio
    raise ValueError("portfolio not found")


def create_portfolio(name: str, *, base_currency: str = "HKD", cash: float = 0.0) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        now = _now()
        portfolio = {
            "id": _new_id(),
            "name": str(name or "未命名模拟盘").strip()[:40] or "未命名模拟盘",
            "base_currency": str(base_currency or "HKD").strip().upper(),
            "cash": max(0.0, _num(cash, 0)),
            "cash_by_currency": {str(base_currency or "HKD").strip().upper(): max(0.0, _num(cash, 0))},
            "fx_to_hkd": dict(DEFAULT_FX_TO_HKD),
            "apply_mode": "manual",
            "portfolio_kind": "paper",
            "strategy_profile": "general",
            "strategy_tags": [],
            "strategy_hypothesis": {},
            "prompt_template": "",
            "risk_overrides": {},
            "futu_sync_enabled": False,
            "positions": [],
            "trades": [],
            "operations": [
                _normalize_operation(
                    {
                        "type": "portfolio",
                        "source": "user",
                        "title": "创建组合",
                        "summary": f"创建 {str(name or '').strip()[:40] or '未命名模拟盘'}",
                        "created_at": now,
                    }
                )
            ],
            "futu_sync_orders": [],
            "created_at": now,
            "updated_at": now,
        }
        store["portfolios"].append(portfolio)
        store["active_id"] = portfolio["id"]
        return _save_portfolios_unlocked(store)


def delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        portfolio_id = str(portfolio_id or "")
        if len(store["portfolios"]) <= 1:
            raise ValueError("cannot delete the last portfolio")
        store["portfolios"] = [item for item in store["portfolios"] if item["id"] != portfolio_id]
        if len(store["portfolios"]) == 0:
            raise ValueError("portfolio not found")
        if store["active_id"] == portfolio_id:
            store["active_id"] = store["portfolios"][0]["id"]
        return _save_portfolios_unlocked(store)


def set_active_portfolio(portfolio_id: str) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        portfolio_ids = {item["id"] for item in store["portfolios"]}
        if portfolio_id not in portfolio_ids:
            raise ValueError("portfolio not found")
        store["active_id"] = portfolio_id
        return _save_portfolios_unlocked(store)


def clone_portfolio(portfolio_id: str | None, *, name: str = "", apply_mode: str | None = None) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        source_id = str(portfolio_id or store["active_id"])
        source = next((item for item in store["portfolios"] if item["id"] == source_id), None)
        if not source:
            raise ValueError("portfolio not found")
        now = _now()
        clone = {
            "id": _new_id(),
            "name": (str(name or "").strip()[:40] or f"{source['name']} Copy")[:40],
            "base_currency": source.get("base_currency", "HKD"),
            "cash": source.get("cash", 0),
            "cash_by_currency": dict(source.get("cash_by_currency") or {}),
            "fx_to_hkd": dict(source.get("fx_to_hkd") or DEFAULT_FX_TO_HKD),
            "apply_mode": _normalize_apply_mode(apply_mode or source.get("apply_mode")),
            "portfolio_kind": source.get("portfolio_kind", "paper"),
            "strategy_profile": _normalize_strategy_profile(source.get("strategy_profile")),
            "strategy_tags": list(source.get("strategy_tags") or []),
            "strategy_hypothesis": _normalize_strategy_hypothesis(source.get("strategy_hypothesis")),
            "prompt_template": _normalize_prompt_template(source.get("prompt_template")),
            "risk_overrides": _normalize_risk_overrides(source.get("risk_overrides")),
            "futu_sync_enabled": False,
            "parent_id": source.get("id", ""),
            "positions": [dict(position) for position in source.get("positions", [])],
            "trades": [],
            "operations": [
                _normalize_operation(
                    {
                        "type": "portfolio",
                        "source": "user",
                        "title": "克隆组合",
                        "summary": f"从 {source.get('name', '')} 克隆",
                        "payload": {"parent_id": source.get("id", ""), "apply_mode": _normalize_apply_mode(apply_mode or source.get("apply_mode"))},
                        "created_at": now,
                    }
                )
            ],
            "futu_sync_orders": [],
            "created_at": now,
            "updated_at": now,
        }
        store["portfolios"].append(clone)
        store["active_id"] = clone["id"]
        return _save_portfolios_unlocked(store)


def update_portfolio_settings(
    portfolio_id: str | None,
    *,
    apply_mode: str | None = None,
    portfolio_kind: str | None = None,
    futu_sync_enabled: bool | None = None,
    strategy_profile: str | None = None,
    strategy_tags: list[str] | str | None = None,
    strategy_hypothesis: dict[str, Any] | str | None = None,
    prompt_template: str | None = None,
    risk_overrides: dict[str, Any] | None = None,
    ai_loop_enabled: bool | None = None,
) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue
            changes: dict[str, Any] = {}
            if apply_mode is not None:
                next_apply_mode = _normalize_apply_mode(apply_mode)
                if portfolio.get("apply_mode") != next_apply_mode:
                    changes["apply_mode"] = {"from": portfolio.get("apply_mode"), "to": next_apply_mode}
                    portfolio["apply_mode"] = next_apply_mode
            if portfolio_kind is not None:
                next_kind = _normalize_portfolio_kind(portfolio_kind)
                if _normalize_portfolio_kind(portfolio.get("portfolio_kind")) != next_kind:
                    changes["portfolio_kind"] = {
                        "from": _portfolio_kind_label(portfolio.get("portfolio_kind")),
                        "to": _portfolio_kind_label(next_kind),
                    }
                    portfolio["portfolio_kind"] = next_kind
                    portfolio["portfolio_kind_label"] = _portfolio_kind_label(next_kind)
            if futu_sync_enabled is not None:
                next_sync = bool(futu_sync_enabled)
                if bool(portfolio.get("futu_sync_enabled")) != next_sync:
                    changes["futu_sync_enabled"] = {"from": bool(portfolio.get("futu_sync_enabled")), "to": next_sync}
                    portfolio["futu_sync_enabled"] = next_sync
            if strategy_profile is not None:
                next_profile = _normalize_strategy_profile(strategy_profile)
                if _normalize_strategy_profile(portfolio.get("strategy_profile")) != next_profile:
                    changes["strategy_profile"] = {"from": portfolio.get("strategy_profile") or "general", "to": next_profile}
                    portfolio["strategy_profile"] = next_profile
            if strategy_tags is not None:
                next_tags = _normalize_strategy_tags(strategy_tags)
                current_tags = _normalize_strategy_tags(portfolio.get("strategy_tags"))
                if current_tags != next_tags:
                    changes["strategy_tags"] = {"from": current_tags, "to": next_tags}
                    portfolio["strategy_tags"] = next_tags
            if strategy_hypothesis is not None:
                next_hypothesis = _normalize_strategy_hypothesis(strategy_hypothesis)
                current_hypothesis = _normalize_strategy_hypothesis(portfolio.get("strategy_hypothesis"))
                if current_hypothesis != next_hypothesis:
                    changes["strategy_hypothesis"] = {"from": current_hypothesis, "to": next_hypothesis}
                    portfolio["strategy_hypothesis"] = next_hypothesis
            if prompt_template is not None:
                next_prompt = _normalize_prompt_template(prompt_template)
                current_prompt = _normalize_prompt_template(portfolio.get("prompt_template"))
                if current_prompt != next_prompt:
                    changes["prompt_template"] = {
                        "from": bool(current_prompt),
                        "to": bool(next_prompt),
                    }
                    portfolio["prompt_template"] = next_prompt
            if risk_overrides is not None:
                next_risk_overrides = _normalize_risk_overrides(risk_overrides)
                current_risk_overrides = _normalize_risk_overrides(portfolio.get("risk_overrides"))
                if current_risk_overrides != next_risk_overrides:
                    changes["risk_overrides"] = {"from": current_risk_overrides, "to": next_risk_overrides}
                    portfolio["risk_overrides"] = next_risk_overrides
            if ai_loop_enabled is not None:
                next_ai_loop_enabled = bool(ai_loop_enabled)
                if bool(portfolio.get("ai_loop_enabled", True)) != next_ai_loop_enabled:
                    changes["ai_loop_enabled"] = {
                        "from": bool(portfolio.get("ai_loop_enabled", True)),
                        "to": next_ai_loop_enabled,
                    }
                    portfolio["ai_loop_enabled"] = next_ai_loop_enabled
            if changes:
                summary_parts = []
                if "portfolio_kind" in changes:
                    summary_parts.append(f"口径 {changes['portfolio_kind']['from']} -> {changes['portfolio_kind']['to']}")
                if "apply_mode" in changes:
                    summary_parts.append(f"AI模式 {changes['apply_mode']['from']} -> {changes['apply_mode']['to']}")
                if "futu_sync_enabled" in changes:
                    summary_parts.append(f"富途同步 {'开启' if changes['futu_sync_enabled']['to'] else '关闭'}")
                if "strategy_profile" in changes:
                    summary_parts.append(f"策略 {changes['strategy_profile']['from']} -> {changes['strategy_profile']['to']}")
                if "strategy_tags" in changes:
                    summary_parts.append(f"标签 {', '.join(changes['strategy_tags']['to']) or '清空'}")
                if "strategy_hypothesis" in changes:
                    benchmark = changes["strategy_hypothesis"]["to"].get("benchmark", "")
                    summary_parts.append(f"策略假设已更新{f'（基准 {benchmark}）' if benchmark else ''}")
                if "prompt_template" in changes:
                    summary_parts.append("提示模板已更新" if changes["prompt_template"]["to"] else "提示模板已清空")
                if "risk_overrides" in changes:
                    summary_parts.append("组合风控覆盖已更新" if changes["risk_overrides"]["to"] else "组合风控覆盖已清空")
                if "ai_loop_enabled" in changes:
                    summary_parts.append(f"AI循环 {'开启' if changes['ai_loop_enabled']['to'] else '关闭'}")
                _append_operation(
                    portfolio,
                    {
                        "type": "settings",
                        "source": "user",
                        "title": "更新组合设置",
                        "summary": "；".join(summary_parts),
                        "payload": changes,
                    },
                )
            portfolio["updated_at"] = _now()
            store["active_id"] = target_id
            return _save_portfolios_unlocked(store)
    raise ValueError("portfolio not found")


def record_futu_sync_order(portfolio_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        sync_order = _normalize_sync_order(payload)
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue
            orders = [
                _normalize_sync_order(item)
                for item in portfolio.get("futu_sync_orders", [])
                if isinstance(item, dict)
            ]
            order_id = sync_order.get("order_id")
            if order_id:
                orders = [item for item in orders if item.get("order_id") != order_id]
            orders.append(sync_order)
            portfolio["futu_sync_orders"] = orders[-500:]
            portfolio["updated_at"] = _now()
            return _save_portfolios_unlocked(store)
    raise ValueError("portfolio not found")


def update_futu_sync_order(portfolio_id: str | None, order_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        order_id = str(order_id or "")
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue
            orders = [
                _normalize_sync_order(item)
                for item in portfolio.get("futu_sync_orders", [])
                if isinstance(item, dict)
            ]
            for order in orders:
                if str(order.get("order_id") or "") != order_id:
                    continue
                order.update(updates)
                order["updated_at"] = _now()
                portfolio["futu_sync_orders"] = [_normalize_sync_order(item) for item in orders][-500:]
                portfolio["updated_at"] = _now()
                return _save_portfolios_unlocked(store)
            raise ValueError("sync order not found")
    raise ValueError("portfolio not found")


def update_portfolio_cash(portfolio_id: str | None, cash: Any, *, currency: str | None = None) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue
            base_currency = str(portfolio.get("base_currency") or "HKD").upper()
            target_currency = str(currency or base_currency).strip().upper() or base_currency
            next_cash = max(0.0, _num(cash, 0))
            cash_by_currency = dict(portfolio.get("cash_by_currency") or {})
            previous_cash = _num(cash_by_currency.get(target_currency), 0)
            cash_delta = round(next_cash - previous_cash, 4)
            fx_rates = _fx_rates_to_hkd(portfolio)
            cash_by_currency[target_currency] = next_cash
            portfolio["cash"] = _num(cash_by_currency.get(base_currency), 0)
            portfolio["cash_by_currency"] = cash_by_currency
            if abs(cash_delta) > 1e-9:
                _append_operation(
                    portfolio,
                    {
                        "type": "cash",
                        "source": "user",
                        "title": "更新现金",
                        "summary": f"{target_currency} {previous_cash:g} -> {next_cash:g}",
                        "currency": target_currency,
                        "payload": {
                            "from": previous_cash,
                            "to": next_cash,
                            "cash_flow": cash_delta,
                            "cash_flow_hkd": round(cash_delta * _num(fx_rates.get(target_currency), 0), 4),
                            "flow_type": "external_cash_flow",
                        },
                    },
                )
            portfolio["updated_at"] = _now()
            store["active_id"] = target_id
            return _save_portfolios_unlocked(store)
    raise ValueError("portfolio not found")


def update_portfolio_fx_rates(portfolio_id: str | None, fx_to_hkd: dict[str, Any]) -> dict[str, Any]:
    next_rates = dict(DEFAULT_FX_TO_HKD)
    next_rates.update(_normalized_fx_rates(fx_to_hkd))
    next_rates["HKD"] = 1.0
    if _num(next_rates.get("USD"), 0) <= 0:
        raise ValueError("USD/HKD rate must be greater than 0")

    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue
            previous_rates = _fx_rates_to_hkd(portfolio)
            rounded_rates = {currency: round(value, 6) for currency, value in sorted(next_rates.items())}
            portfolio["fx_to_hkd"] = rounded_rates
            portfolio["fx_source"] = BROKER_MANUAL_FX_SOURCE
            portfolio["fx_updated_at"] = _now()
            portfolio["fx_status"] = {
                "ok": True,
                "source": BROKER_MANUAL_FX_SOURCE,
                "updated_at": portfolio["fx_updated_at"],
                "error": "",
            }
            _append_operation(
                portfolio,
                {
                    "type": "fx",
                    "source": "user",
                    "title": "更新券商校准汇率",
                    "summary": "；".join(
                        f"{currency} {previous_rates.get(currency, DEFAULT_FX_TO_HKD.get(currency, 0)):g} -> {value:g}"
                        for currency, value in rounded_rates.items()
                        if currency != "HKD"
                    ),
                    "payload": {
                        "from": {currency: round(value, 6) for currency, value in sorted(previous_rates.items())},
                        "to": rounded_rates,
                        "source": BROKER_MANUAL_FX_SOURCE,
                    },
                },
            )
            portfolio["updated_at"] = _now()
            return _save_portfolios_unlocked(store)
    raise ValueError("portfolio not found")


def upsert_position(portfolio_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue
            code = _normalize_code(payload.get("code", ""))
            existing_by_code = {position["code"]: position for position in portfolio.get("positions", [])}
            existing = existing_by_code.get(code)
            position = _normalize_position(payload, existing)
            portfolio["positions"] = [item for item in portfolio.get("positions", []) if item["code"] != code]
            portfolio["positions"].append(position)
            portfolio["positions"].sort(key=lambda item: item["code"])
            _append_operation(
                portfolio,
                {
                    "type": "position",
                    "source": "user",
                    "title": "编辑持仓快照" if existing else "新增持仓快照",
                    "summary": f"{position['code']} 数量 {position['qty']:g} 成本 {position['cost_price']:g} {position['currency']}",
                    "code": position["code"],
                    "qty": position["qty"],
                    "price": position["cost_price"],
                    "currency": position["currency"],
                    "payload": {"before": existing or {}, "after": position},
                },
            )
            portfolio["updated_at"] = _now()
            store["active_id"] = target_id
            return _save_portfolios_unlocked(store)
    raise ValueError("portfolio not found")


def delete_position(portfolio_id: str | None, code: str) -> dict[str, Any]:
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        normalized_code = _normalize_code(code)
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue
            before = len(portfolio.get("positions", []))
            removed = next((item for item in portfolio.get("positions", []) if item.get("code") == normalized_code), None)
            portfolio["positions"] = [item for item in portfolio.get("positions", []) if item["code"] != normalized_code]
            if len(portfolio["positions"]) == before:
                raise ValueError("position not found")
            _append_operation(
                portfolio,
                {
                    "type": "position",
                    "source": "user",
                    "title": "删除持仓快照",
                    "summary": normalized_code,
                    "code": normalized_code,
                    "payload": {"before": removed or {}},
                },
            )
            portfolio["updated_at"] = _now()
            return _save_portfolios_unlocked(store)
    raise ValueError("portfolio not found")


def apply_order_to_portfolio(
    portfolio_id: str | None,
    order_payload: dict[str, Any],
    *,
    source: str = "manual",
    decision_id: str = "",
    reason: str = "",
    fx_to_hkd: dict[str, Any] | None = None,
    fx_source: str = "",
    fx_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = OrderIntent.from_dict(order_payload)
    with file_lock(PORTFOLIOS_PATH):
        store = _load_store_unlocked()
        target_id = str(portfolio_id or store["active_id"])
        for portfolio in store["portfolios"]:
            if portfolio["id"] != target_id:
                continue

            trades = list(portfolio.get("trades") or [])
            if decision_id and any(trade.get("decision_id") == decision_id for trade in trades):
                return {
                    "ok": True,
                    "status": "already_applied",
                    "portfolio_id": target_id,
                    "decision_id": decision_id,
                    "message": "Decision has already been applied to this local portfolio.",
                }

            source_key = str(source or "").strip().lower()
            if source_key not in {"user_trade", "futu_sync"}:
                session = market_session_payload(intent.market)
                if not session["can_trade"]:
                    raise ValueError(
                        f"market session blocked: {intent.market} is {session['status']} "
                        f"({session['local_time']} {session['timezone']}; {session['reason']}). "
                        "AI/manual decision applications are only allowed during regular trading sessions. "
                        "Use the portfolio trade recorder for real broker fills."
                    )

            currency = _currency_for_market(intent.market)
            base_currency = str(portfolio.get("base_currency") or "HKD").upper()
            fx_payload = dict(portfolio)
            if fx_to_hkd:
                merged_rates = dict(portfolio.get("fx_to_hkd") or {})
                merged_rates.update(fx_to_hkd)
                fx_payload["fx_to_hkd"] = merged_rates
            fx_rates = _fx_rates_to_hkd(fx_payload)
            fx_source_name = str(fx_source or LOCAL_DEFAULT_FX_SOURCE)
            cash_by_currency = dict(portfolio.get("cash_by_currency") or {})
            cash_by_currency.setdefault(currency, 0.0)
            positions = [dict(position) for position in portfolio.get("positions", [])]
            existing = next((position for position in positions if position.get("code") == intent.code), None)
            notional = round(intent.notional, 4)
            fee_payload = _trade_fee(intent.market, intent.side, intent.qty, notional)
            fees = _num(fee_payload.get("total"), 0)
            fee_bps = _num(fee_payload.get("bps"), 0)
            fee_model = str(fee_payload.get("model") or LOCAL_FEE_MODEL)
            fee_details = list(fee_payload.get("details") or [])
            realized_pnl = 0.0
            cash_effects: list[dict[str, Any]] = []
            fx_detail: dict[str, Any] = {}
            net_cash_amount = 0.0

            if source_key != "user_trade":
                risk_config = risk_config_with_overrides(AppConfig.from_env().risk, portfolio.get("risk_overrides"))
                risk_decision = RiskEngine(risk_config).validate_portfolio(
                    intent,
                    portfolio,
                    positions=positions,
                    fx_to_hkd=fx_rates,
                )
                if not risk_decision.approved:
                    raise ValueError("portfolio risk blocked: " + "; ".join(risk_decision.violations))

            if intent.side == "BUY":
                cash_needed = round(notional + fees, 4)
                available_cash = _num(cash_by_currency.get(currency), 0)
                direct_spend = min(available_cash, cash_needed)
                if direct_spend > 0:
                    cash_by_currency[currency] = round(available_cash - direct_spend, 4)
                    cash_effects.append(_cash_effect(currency, -direct_spend, "buy_cost_with_fees"))
                remaining_cash_needed = round(cash_needed - direct_spend, 4)
                if remaining_cash_needed > 0:
                    if base_currency == currency:
                        raise ValueError(f"insufficient {currency} cash: need {cash_needed}, available {available_cash}")
                    source_amount = round(_convert_currency(remaining_cash_needed, currency, base_currency, fx_rates), 4)
                    source_cash = _num(cash_by_currency.get(base_currency), 0)
                    if source_cash + 1e-9 < source_amount:
                        raise ValueError(
                            f"insufficient buying power: need {remaining_cash_needed} {currency} "
                            f"({source_amount} {base_currency}), available {source_cash} {base_currency}"
                        )
                    cash_by_currency[base_currency] = round(source_cash - source_amount, 4)
                    cash_effects.append(
                        _cash_effect(
                            base_currency,
                            -source_amount,
                            "auto_fx",
                            target_currency=currency,
                            target_amount=remaining_cash_needed,
                        )
                    )
                    fx_detail = {
                        "source_currency": base_currency,
                        "target_currency": currency,
                        "source_amount": source_amount,
                        "target_amount": remaining_cash_needed,
                        "rate": round(source_amount / remaining_cash_needed, 6),
                        "source": fx_source_name,
                    }
                cost_basis = round(notional + fees, 4)
                net_cash_amount = -cost_basis
                if existing:
                    old_qty = _num(existing.get("qty"), 0)
                    old_cost = _num(existing.get("cost_price"), 0)
                    next_qty = old_qty + intent.qty
                    next_cost = ((old_qty * old_cost) + cost_basis) / next_qty if next_qty > 0 else intent.price
                    existing["qty"] = round(next_qty, 4)
                    existing["cost_price"] = round(next_cost, 4)
                    existing["updated_at"] = _now()
                else:
                    default_note = "本人交易记录" if source_key == "user_trade" else "AI applied local trade"
                    positions.append(
                        _normalize_position(
                            {
                                "code": intent.code,
                                "qty": intent.qty,
                                "cost_price": round(cost_basis / intent.qty, 4) if intent.qty > 0 else intent.price,
                                "currency": currency,
                                "note": default_note,
                            }
                        )
                    )
            elif intent.side == "SELL":
                if not existing:
                    raise ValueError("SELL blocked because local portfolio has no matching position")
                old_qty = _num(existing.get("qty"), 0)
                if old_qty + 1e-9 < intent.qty:
                    raise ValueError(f"SELL blocked because qty {intent.qty} exceeds local holding {old_qty}")
                avg_cost = _num(existing.get("cost_price"), 0)
                position_currency = str(existing.get("currency") or currency).upper()
                cash_by_currency.setdefault(position_currency, 0.0)
                net_proceeds = round(notional - fees, 4)
                realized_pnl = round(net_proceeds - avg_cost * intent.qty, 4)
                cash_by_currency[position_currency] = round(_num(cash_by_currency.get(position_currency), 0) + net_proceeds, 4)
                cash_effects.append(_cash_effect(position_currency, net_proceeds, "sell_proceeds_after_fees"))
                remaining_qty = round(old_qty - intent.qty, 4)
                if remaining_qty > 0:
                    existing["qty"] = remaining_qty
                    existing["updated_at"] = _now()
                else:
                    positions = [position for position in positions if position.get("code") != intent.code]
                currency = position_currency
                net_cash_amount = net_proceeds
            else:
                raise ValueError("side must be BUY or SELL")

            trade = {
                "id": _new_id(),
                "decision_id": decision_id,
                "source": str(source or "manual"),
                "side": intent.side,
                "code": intent.code,
                "qty": round(intent.qty, 4),
                "price": round(intent.price, 4),
                "currency": currency,
                "notional": notional,
                "fees": fees,
                "fee_currency": currency,
                "fee_bps": fee_bps,
                "fee_model": fee_model,
                "fee_details": fee_details,
                "net_cash_amount": net_cash_amount,
                "realized_pnl": realized_pnl,
                "cash_effects": cash_effects,
                "fx": fx_detail,
                "reason": reason or intent.reason,
                "created_at": _now(),
            }
            trades.append(trade)
            _append_operation(portfolio, _operation_from_trade(trade))

            portfolio["cash_by_currency"] = cash_by_currency
            portfolio["cash"] = round(_num(cash_by_currency.get(base_currency), 0), 4)
            portfolio["fx_to_hkd"] = {currency: round(value, 6) for currency, value in sorted(fx_rates.items())}
            portfolio["fx_source"] = fx_source_name
            if fx_status:
                portfolio["fx_status"] = {
                    "ok": bool(fx_status.get("ok")),
                    "source": str(fx_status.get("source") or fx_source_name),
                    "error": str(fx_status.get("error") or ""),
                    "updated_at": str(fx_status.get("updated_at") or ""),
                }
            portfolio["positions"] = sorted(positions, key=lambda item: item.get("code", ""))
            portfolio["trades"] = trades[-500:]
            portfolio["updated_at"] = _now()
            _save_portfolios_unlocked(store)
            return {
                "ok": True,
                "status": "applied",
                "portfolio_id": target_id,
                "decision_id": decision_id,
                "trade": trade,
                "cash_by_currency": dict(sorted(cash_by_currency.items())),
                "fx": {
                    "source": fx_source_name,
                    "fx_to_hkd": dict(sorted(fx_rates.items())),
                    "status": dict(fx_status or {}),
                },
            }
    raise ValueError("portfolio not found")


def portfolio_context(portfolio_id: str | None = None) -> dict[str, Any]:
    store = load_portfolios()
    active = get_portfolio(portfolio_id or store["active_id"])
    return {
        "active_id": active["id"],
        "portfolio": {
            "id": active["id"],
            "name": active["name"],
            "portfolio_kind": active.get("portfolio_kind", "paper"),
            "portfolio_kind_label": active.get("portfolio_kind_label") or _portfolio_kind_label(active.get("portfolio_kind")),
            "base_currency": active["base_currency"],
            "cash": active["cash"],
            "cash_by_currency": active.get("cash_by_currency", {}),
            "fx_to_hkd": active.get("fx_to_hkd", {}),
            "buying_power_rule": "本地账本买入跨币种资产时，可以按汇率从基础币种现金自动换汇扣款。",
            "apply_mode": active.get("apply_mode", "manual"),
            "strategy_profile": active.get("strategy_profile", "general"),
            "strategy_tags": list(active.get("strategy_tags", [])),
            "strategy_hypothesis": dict(active.get("strategy_hypothesis") or {}),
            "prompt_template": str(active.get("prompt_template") or ""),
            "risk_overrides": dict(active.get("risk_overrides") or {}),
            "recent_operations": list(active.get("operations", []))[-20:],
            "updated_at": active["updated_at"],
        },
        "positions": list(active.get("positions", [])),
    }
