from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .config import STATE_ROOT
from .models import infer_market
from .news_signals import normalize_ticker


PORTFOLIOS_PATH = STATE_ROOT / "portfolios.json"
DEFAULT_PORTFOLIO_ID = "default"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _currency_for_market(market: str) -> str:
    return {"US": "USD", "HK": "HKD", "CN": "CNY"}.get(market.upper(), "USD")


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
                "positions": [],
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
    positions = []
    for raw_position in payload.get("positions") or []:
        if isinstance(raw_position, dict):
            positions.append(_normalize_position(raw_position))
    return {
        "id": portfolio_id,
        "name": name,
        "base_currency": base_currency,
        "cash": max(0.0, _num(payload.get("cash", 0))),
        "positions": positions,
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
        "positions": [],
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
            "updated_at": active["updated_at"],
        },
        "positions": list(active.get("positions", [])),
    }
