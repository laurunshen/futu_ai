from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .config import STATE_ROOT
from .models import OrderIntent, infer_market
from .news_signals import normalize_ticker


PORTFOLIOS_PATH = STATE_ROOT / "portfolios.json"
DEFAULT_PORTFOLIO_ID = "default"
APPLY_MODES = {"observe", "manual", "auto"}
PORTFOLIO_KINDS = {"paper", "actual"}
DEFAULT_FX_TO_HKD = {"HKD": 1.0, "USD": 7.8, "CNY": 1.08, "CNH": 1.08}
STRATEGY_PROFILES = {"general", "short_swing", "long_hold", "growth_aggressive", "defensive_cashflow", "news_driven"}


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
            "payload": {"reason": trade.get("reason"), "cash_effects": trade.get("cash_effects"), "fx": trade.get("fx")},
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
        "fx_source": str(payload.get("fx_source") or "local_default_fx_to_hkd"),
        "fx_status": dict(payload.get("fx_status") or {}),
        "apply_mode": _normalize_apply_mode(payload.get("apply_mode")),
        "strategy_profile": _normalize_strategy_profile(payload.get("strategy_profile")),
        "strategy_tags": _normalize_strategy_tags(payload.get("strategy_tags")),
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


def _load_store() -> dict[str, Any]:
    if not PORTFOLIOS_PATH.exists():
        store = _default_store()
        save_portfolios(store)
        return store
    try:
        payload = json.loads(PORTFOLIOS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return _normalize_store(payload if isinstance(payload, dict) else {})


def save_portfolios(store: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_store(store)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    PORTFOLIOS_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


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
    store = load_portfolios()
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
    return save_portfolios(store)


def delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    store = load_portfolios()
    portfolio_id = str(portfolio_id or "")
    if len(store["portfolios"]) <= 1:
        raise ValueError("cannot delete the last portfolio")
    store["portfolios"] = [item for item in store["portfolios"] if item["id"] != portfolio_id]
    if len(store["portfolios"]) == 0:
        raise ValueError("portfolio not found")
    if store["active_id"] == portfolio_id:
        store["active_id"] = store["portfolios"][0]["id"]
    return save_portfolios(store)


def set_active_portfolio(portfolio_id: str) -> dict[str, Any]:
    store = load_portfolios()
    portfolio_ids = {item["id"] for item in store["portfolios"]}
    if portfolio_id not in portfolio_ids:
        raise ValueError("portfolio not found")
    store["active_id"] = portfolio_id
    return save_portfolios(store)


def clone_portfolio(portfolio_id: str | None, *, name: str = "", apply_mode: str | None = None) -> dict[str, Any]:
    store = load_portfolios()
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
    return save_portfolios(store)


def update_portfolio_settings(
    portfolio_id: str | None,
    *,
    apply_mode: str | None = None,
    portfolio_kind: str | None = None,
    futu_sync_enabled: bool | None = None,
    strategy_profile: str | None = None,
    strategy_tags: list[str] | str | None = None,
) -> dict[str, Any]:
    store = load_portfolios()
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
        return save_portfolios(store)
    raise ValueError("portfolio not found")


def record_futu_sync_order(portfolio_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    store = load_portfolios()
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
        return save_portfolios(store)
    raise ValueError("portfolio not found")


def update_futu_sync_order(portfolio_id: str | None, order_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    store = load_portfolios()
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
            return save_portfolios(store)
        raise ValueError("sync order not found")
    raise ValueError("portfolio not found")


def update_portfolio_cash(portfolio_id: str | None, cash: Any, *, currency: str | None = None) -> dict[str, Any]:
    store = load_portfolios()
    target_id = str(portfolio_id or store["active_id"])
    for portfolio in store["portfolios"]:
        if portfolio["id"] != target_id:
            continue
        base_currency = str(portfolio.get("base_currency") or "HKD").upper()
        target_currency = str(currency or base_currency).strip().upper() or base_currency
        next_cash = max(0.0, _num(cash, 0))
        cash_by_currency = dict(portfolio.get("cash_by_currency") or {})
        previous_cash = _num(cash_by_currency.get(target_currency), 0)
        cash_by_currency[target_currency] = next_cash
        portfolio["cash"] = _num(cash_by_currency.get(base_currency), 0)
        portfolio["cash_by_currency"] = cash_by_currency
        if abs(previous_cash - next_cash) > 1e-9:
            _append_operation(
                portfolio,
                {
                    "type": "cash",
                    "source": "user",
                    "title": "更新现金",
                    "summary": f"{target_currency} {previous_cash:g} -> {next_cash:g}",
                    "currency": target_currency,
                    "payload": {"from": previous_cash, "to": next_cash},
                },
            )
        portfolio["updated_at"] = _now()
        store["active_id"] = target_id
        return save_portfolios(store)
    raise ValueError("portfolio not found")


def upsert_position(portfolio_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    store = load_portfolios()
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
        return save_portfolios(store)
    raise ValueError("portfolio not found")


def delete_position(portfolio_id: str | None, code: str) -> dict[str, Any]:
    store = load_portfolios()
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
        return save_portfolios(store)
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
    store = load_portfolios()
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

        currency = _currency_for_market(intent.market)
        base_currency = str(portfolio.get("base_currency") or "HKD").upper()
        fx_payload = dict(portfolio)
        if fx_to_hkd:
            merged_rates = dict(portfolio.get("fx_to_hkd") or {})
            merged_rates.update(fx_to_hkd)
            fx_payload["fx_to_hkd"] = merged_rates
        fx_rates = _fx_rates_to_hkd(fx_payload)
        fx_source_name = str(fx_source or "local_default_fx_to_hkd")
        cash_by_currency = dict(portfolio.get("cash_by_currency") or {})
        cash_by_currency.setdefault(currency, 0.0)
        positions = [dict(position) for position in portfolio.get("positions", [])]
        existing = next((position for position in positions if position.get("code") == intent.code), None)
        notional = round(intent.notional, 4)
        realized_pnl = 0.0
        cash_effects: list[dict[str, Any]] = []
        fx_detail: dict[str, Any] = {}

        if intent.side == "BUY":
            available_cash = _num(cash_by_currency.get(currency), 0)
            direct_spend = min(available_cash, notional)
            if direct_spend > 0:
                cash_by_currency[currency] = round(available_cash - direct_spend, 4)
                cash_effects.append(_cash_effect(currency, -direct_spend, "trade_currency"))
            remaining_notional = round(notional - direct_spend, 4)
            if remaining_notional > 0:
                if base_currency == currency:
                    raise ValueError(f"insufficient {currency} cash: need {notional}, available {available_cash}")
                source_amount = round(_convert_currency(remaining_notional, currency, base_currency, fx_rates), 4)
                source_cash = _num(cash_by_currency.get(base_currency), 0)
                if source_cash + 1e-9 < source_amount:
                    raise ValueError(
                        f"insufficient buying power: need {remaining_notional} {currency} "
                        f"({source_amount} {base_currency}), available {source_cash} {base_currency}"
                    )
                cash_by_currency[base_currency] = round(source_cash - source_amount, 4)
                cash_effects.append(
                    _cash_effect(
                        base_currency,
                        -source_amount,
                        "auto_fx",
                        target_currency=currency,
                        target_amount=remaining_notional,
                    )
                )
                fx_detail = {
                    "source_currency": base_currency,
                    "target_currency": currency,
                    "source_amount": source_amount,
                    "target_amount": remaining_notional,
                    "rate": round(source_amount / remaining_notional, 6),
                    "source": fx_source_name,
                }
            if existing:
                old_qty = _num(existing.get("qty"), 0)
                old_cost = _num(existing.get("cost_price"), 0)
                next_qty = old_qty + intent.qty
                next_cost = ((old_qty * old_cost) + notional) / next_qty if next_qty > 0 else intent.price
                existing["qty"] = round(next_qty, 4)
                existing["cost_price"] = round(next_cost, 4)
                existing["updated_at"] = _now()
            else:
                default_note = "本人交易记录" if str(source or "").lower() == "user_trade" else "AI applied local trade"
                positions.append(
                    _normalize_position(
                        {
                            "code": intent.code,
                            "qty": intent.qty,
                            "cost_price": intent.price,
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
            realized_pnl = round((intent.price - avg_cost) * intent.qty, 4)
            cash_by_currency[position_currency] = round(_num(cash_by_currency.get(position_currency), 0) + notional, 4)
            cash_effects.append(_cash_effect(position_currency, notional, "sell_proceeds"))
            remaining_qty = round(old_qty - intent.qty, 4)
            if remaining_qty > 0:
                existing["qty"] = remaining_qty
                existing["updated_at"] = _now()
            else:
                positions = [position for position in positions if position.get("code") != intent.code]
            currency = position_currency
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
        save_portfolios(store)
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
            "recent_operations": list(active.get("operations", []))[-20:],
            "updated_at": active["updated_at"],
        },
        "positions": list(active.get("positions", [])),
    }
