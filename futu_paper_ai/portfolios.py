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


def _normalize_apply_mode(value: Any) -> str:
    mode = str(value or "manual").strip().lower()
    return mode if mode in APPLY_MODES else "manual"


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
        "reason": str(payload.get("reason") or ""),
        "created_at": str(payload.get("created_at") or _now()),
    }


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
                "apply_mode": "manual",
                "futu_sync_enabled": False,
                "positions": [],
                "trades": [],
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
    return {
        "id": portfolio_id,
        "name": name,
        "base_currency": base_currency,
        "cash": round(cash_by_currency.get(base_currency, 0.0), 4),
        "cash_by_currency": {currency: round(value, 4) for currency, value in sorted(cash_by_currency.items())},
        "apply_mode": _normalize_apply_mode(payload.get("apply_mode")),
        "futu_sync_enabled": bool(payload.get("futu_sync_enabled", False)),
        "parent_id": str(payload.get("parent_id") or ""),
        "positions": positions,
        "trades": trades,
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
        "apply_mode": "manual",
        "futu_sync_enabled": False,
        "positions": [],
        "trades": [],
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
        "apply_mode": _normalize_apply_mode(apply_mode or source.get("apply_mode")),
        "futu_sync_enabled": False,
        "parent_id": source.get("id", ""),
        "positions": [dict(position) for position in source.get("positions", [])],
        "trades": [],
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
    futu_sync_enabled: bool | None = None,
) -> dict[str, Any]:
    store = load_portfolios()
    target_id = str(portfolio_id or store["active_id"])
    for portfolio in store["portfolios"]:
        if portfolio["id"] != target_id:
            continue
        if apply_mode is not None:
            portfolio["apply_mode"] = _normalize_apply_mode(apply_mode)
        if futu_sync_enabled is not None:
            portfolio["futu_sync_enabled"] = bool(futu_sync_enabled)
        portfolio["updated_at"] = _now()
        store["active_id"] = target_id
        return save_portfolios(store)
    raise ValueError("portfolio not found")


def update_portfolio_cash(portfolio_id: str | None, cash: Any) -> dict[str, Any]:
    store = load_portfolios()
    target_id = str(portfolio_id or store["active_id"])
    for portfolio in store["portfolios"]:
        if portfolio["id"] != target_id:
            continue
        base_currency = str(portfolio.get("base_currency") or "HKD").upper()
        next_cash = max(0.0, _num(cash, 0))
        cash_by_currency = dict(portfolio.get("cash_by_currency") or {})
        cash_by_currency[base_currency] = next_cash
        portfolio["cash"] = next_cash
        portfolio["cash_by_currency"] = cash_by_currency
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
        position = _normalize_position(payload, existing_by_code.get(code))
        portfolio["positions"] = [item for item in portfolio.get("positions", []) if item["code"] != code]
        portfolio["positions"].append(position)
        portfolio["positions"].sort(key=lambda item: item["code"])
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
        portfolio["positions"] = [item for item in portfolio.get("positions", []) if item["code"] != normalized_code]
        if len(portfolio["positions"]) == before:
            raise ValueError("position not found")
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
        cash_by_currency = dict(portfolio.get("cash_by_currency") or {})
        cash_by_currency.setdefault(currency, 0.0)
        positions = [dict(position) for position in portfolio.get("positions", [])]
        existing = next((position for position in positions if position.get("code") == intent.code), None)
        notional = round(intent.notional, 4)
        realized_pnl = 0.0

        if intent.side == "BUY":
            available_cash = _num(cash_by_currency.get(currency), 0)
            if available_cash + 1e-9 < notional:
                raise ValueError(f"insufficient {currency} cash: need {notional}, available {available_cash}")
            cash_by_currency[currency] = round(available_cash - notional, 4)
            if existing:
                old_qty = _num(existing.get("qty"), 0)
                old_cost = _num(existing.get("cost_price"), 0)
                next_qty = old_qty + intent.qty
                next_cost = ((old_qty * old_cost) + notional) / next_qty if next_qty > 0 else intent.price
                existing["qty"] = round(next_qty, 4)
                existing["cost_price"] = round(next_cost, 4)
                existing["updated_at"] = _now()
            else:
                positions.append(
                    _normalize_position(
                        {
                            "code": intent.code,
                            "qty": intent.qty,
                            "cost_price": intent.price,
                            "currency": currency,
                            "note": "AI applied local trade",
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
            "reason": reason or intent.reason,
            "created_at": _now(),
        }
        trades.append(trade)

        base_currency = str(portfolio.get("base_currency") or "HKD").upper()
        portfolio["cash_by_currency"] = cash_by_currency
        portfolio["cash"] = round(_num(cash_by_currency.get(base_currency), 0), 4)
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
            "base_currency": active["base_currency"],
            "cash": active["cash"],
            "cash_by_currency": active.get("cash_by_currency", {}),
            "apply_mode": active.get("apply_mode", "manual"),
            "updated_at": active["updated_at"],
        },
        "positions": list(active.get("positions", [])),
    }
