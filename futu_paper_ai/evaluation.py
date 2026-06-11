from __future__ import annotations

import json
import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import STATE_ROOT
from .models import infer_market
from .portfolios import DEFAULT_FX_TO_HKD


FOLLOWUPS_PATH = STATE_ROOT / "decision_followups.json"
NAV_HISTORY_PATH = STATE_ROOT / "nav_history.json"
HORIZON_DAYS = (1, 3, 7)
NAV_HISTORY_MAX_POINTS = 2000
TRADE_SOURCE_LABELS = {
    "auto": "AI 自动应用",
    "manual": "AI 手动应用",
    "user_trade": "本人交易",
    "futu_sync": "富途成交回写",
}
TRADE_SOURCE_ORDER = ("auto", "manual", "user_trade", "futu_sync")
REVIEW_LABELS = {
    "correct": "正确",
    "too_early": "过早",
    "too_late": "过晚",
    "wrong": "误判",
    "risk_saved_loss": "风控避免损失",
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, "", "N/A"}:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).isoformat()


def decision_evaluation_id(entry: dict[str, Any]) -> str:
    decision_id = str(entry.get("decision_id") or "").strip()
    if decision_id:
        return decision_id
    decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
    portfolio = entry.get("portfolio") if isinstance(entry.get("portfolio"), dict) else {}
    raw = "|".join(
        [
            str(entry.get("log_date") or ""),
            str(entry.get("timestamp") or entry.get("ts") or ""),
            str(portfolio.get("id") or ""),
            str(decision.get("action") or ""),
            str(decision.get("code") or ""),
            str(decision.get("reason") or "")[:120],
        ]
    )
    return f"legacy-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _currency_for_market(market: str) -> str:
    return {"US": "USD", "HK": "HKD", "CN": "CNY"}.get(market.upper(), "USD")


def _currency_for_code(code: str) -> str:
    try:
        return _currency_for_market(infer_market(code))
    except ValueError:
        return "USD"


def _rates_to_hkd(fx_payload: dict[str, Any] | None) -> dict[str, float]:
    rates = dict(DEFAULT_FX_TO_HKD)
    raw = (fx_payload or {}).get("fx_to_hkd")
    if isinstance(raw, dict):
        for currency, value in raw.items():
            code = str(currency or "").upper()
            rate = _num(value, 0)
            if code and rate > 0:
                rates[code] = rate
    return rates


def _to_hkd(amount: float, currency: str, rates: dict[str, float]) -> float:
    rate = _num(rates.get(str(currency or "").upper()), 0)
    return amount * rate if rate > 0 else 0.0


def quote_price(row: dict[str, Any] | None) -> float:
    row = row or {}
    bid = _num(row.get("bid_price"), 0)
    ask = _num(row.get("ask_price"), 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    for key in ("last_price", "nominal_price", "price", "open_price", "prev_close_price"):
        price = _num(row.get(key), 0)
        if price > 0:
            return price
    return 0.0


def _position_price(position: dict[str, Any], quote: dict[str, Any] | None) -> tuple[float, str, str]:
    live_price = quote_price(quote)
    if live_price > 0:
        return live_price, "Futu OpenD snapshot", str((quote or {}).get("update_time") or "")
    for key in ("last_price", "market_price"):
        price = _num(position.get(key), 0)
        if price > 0:
            return price, str(position.get("price_source") or "decision_snapshot"), str(position.get("quote_update_time") or "")
    cost_price = _num(position.get("cost_price"), 0)
    if cost_price > 0:
        return cost_price, "cost_price_fallback", ""
    return 0.0, "missing", ""


def portfolio_nav_snapshot(
    portfolio: dict[str, Any],
    quote_by_code: dict[str, dict[str, Any]] | None = None,
    fx_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quote_by_code = quote_by_code or {}
    rates = _rates_to_hkd(fx_payload or portfolio)
    base_currency = str(portfolio.get("base_currency") or "HKD").upper()
    raw_cash = portfolio.get("cash_by_currency")
    cash_by_currency = dict(raw_cash) if isinstance(raw_cash, dict) else {base_currency: _num(portfolio.get("cash"), 0)}

    cash_rows: list[dict[str, Any]] = []
    cash_hkd = 0.0
    for currency, value in sorted(cash_by_currency.items()):
        amount = _num(value, 0)
        hkd = _to_hkd(amount, str(currency).upper(), rates)
        cash_hkd += hkd
        cash_rows.append({"currency": str(currency).upper(), "amount": round(amount, 4), "hkd": round(hkd, 4)})

    position_rows: list[dict[str, Any]] = []
    market_value_hkd = 0.0
    cost_value_hkd = 0.0
    missing_quotes: list[str] = []
    for raw_position in portfolio.get("positions") or []:
        if not isinstance(raw_position, dict):
            continue
        position = dict(raw_position)
        code = str(position.get("code") or "").upper()
        qty = _num(position.get("qty"), 0)
        currency = str(position.get("currency") or _currency_for_code(code)).upper()
        cost_price = _num(position.get("cost_price"), 0)
        quote = quote_by_code.get(code, {})
        price, price_source, update_time = _position_price(position, quote)
        if qty > 0 and price_source in {"missing", "cost_price_fallback"}:
            missing_quotes.append(code)
        market_value = qty * price if price > 0 else 0.0
        cost_value = qty * cost_price if cost_price > 0 else 0.0
        market_value_hkd += _to_hkd(market_value, currency, rates)
        cost_value_hkd += _to_hkd(cost_value, currency, rates)
        position_rows.append(
            {
                "code": code,
                "name": position.get("name") or position.get("note") or code,
                "qty": round(qty, 4),
                "currency": currency,
                "price": round(price, 4) if price > 0 else None,
                "price_source": price_source,
                "quote_update_time": update_time,
                "cost_price": round(cost_price, 4) if cost_price > 0 else None,
                "market_value": round(market_value, 4),
                "market_value_hkd": round(_to_hkd(market_value, currency, rates), 4),
                "cost_value_hkd": round(_to_hkd(cost_value, currency, rates), 4),
                "unrealized_pnl_hkd": round(_to_hkd(market_value - cost_value, currency, rates), 4),
            }
        )

    nav_hkd = cash_hkd + market_value_hkd
    unrealized_pnl_hkd = market_value_hkd - cost_value_hkd
    return {
        "base_currency": base_currency,
        "fx_source": str((fx_payload or {}).get("source") or portfolio.get("fx_source") or "local_default_fx_to_hkd"),
        "fx_to_hkd": {currency: round(value, 6) for currency, value in sorted(rates.items())},
        "cash": cash_rows,
        "positions": position_rows,
        "cash_hkd": round(cash_hkd, 4),
        "market_value_hkd": round(market_value_hkd, 4),
        "cost_value_hkd": round(cost_value_hkd, 4),
        "unrealized_pnl_hkd": round(unrealized_pnl_hkd, 4),
        "nav_hkd": round(nav_hkd, 4),
        "missing_quotes": sorted(set(missing_quotes)),
        "estimated": bool(missing_quotes),
    }


def _candidate_map(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("code") or "").upper(): item for item in candidates if isinstance(item, dict)}


def decision_target(entry: dict[str, Any]) -> dict[str, Any]:
    decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
    order = entry.get("order") if isinstance(entry.get("order"), dict) else {}
    code = str(decision.get("code") or "").upper()
    if code and code != "NONE":
        return {"code": code, "source": "decision_code"}
    code = str(order.get("code") or "").upper()
    if code and code != "NONE":
        return {"code": code, "source": "order_code"}
    for candidate in entry.get("candidates") or []:
        if isinstance(candidate, dict) and candidate.get("code"):
            return {"code": str(candidate.get("code")).upper(), "source": "top_candidate_for_hold"}
    return {"code": "", "source": "none"}


def _baseline_price_from_entry(entry: dict[str, Any], code: str) -> dict[str, Any]:
    review = entry.get("review") if isinstance(entry.get("review"), dict) else {}
    decision_price = review.get("decision_price") if isinstance(review.get("decision_price"), dict) else {}
    if str(decision_price.get("code") or "").upper() == code and _num(decision_price.get("price"), 0) > 0:
        return dict(decision_price)

    candidates = _candidate_map(entry.get("candidates") or [])
    candidate = candidates.get(code, {})
    price = _num(candidate.get("last_price") or candidate.get("price"), 0)
    if price <= 0:
        bid = _num(candidate.get("bid_price"), 0)
        ask = _num(candidate.get("ask_price"), 0)
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2
    if price > 0:
        return {
            "code": code,
            "price": round(price, 4),
            "currency": str(candidate.get("currency") or _currency_for_code(code)).upper(),
            "source": "candidate_snapshot",
            "update_time": str(candidate.get("update_time") or entry.get("timestamp") or ""),
        }

    order = entry.get("order") if isinstance(entry.get("order"), dict) else {}
    if str(order.get("code") or "").upper() == code and _num(order.get("price"), 0) > 0:
        return {
            "code": code,
            "price": round(_num(order.get("price")), 4),
            "currency": _currency_for_code(code),
            "source": "order_price",
            "update_time": str(entry.get("timestamp") or ""),
        }
    return {"code": code, "price": None, "currency": _currency_for_code(code), "source": "missing", "update_time": ""}


def _direction(entry: dict[str, Any]) -> int:
    decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
    action = str(decision.get("action") or "").upper()
    rating = str(decision.get("rating") or "").upper()
    position_action = str(decision.get("position_action") or "").upper()
    if action == "BUY" or rating in {"BUY", "OVERWEIGHT"} or position_action in {"ENTER", "ADD"}:
        return 1
    if action == "SELL" or rating in {"SELL", "UNDERWEIGHT"} or position_action in {"TRIM", "EXIT"}:
        return -1
    return 0


def _return_payload(entry: dict[str, Any], baseline_price: float, measured_price: float) -> dict[str, Any]:
    raw_return = ((measured_price - baseline_price) / baseline_price * 100) if baseline_price > 0 else None
    direction = _direction(entry)
    decision_return = raw_return * direction if raw_return is not None and direction else None
    return {
        "raw_return_pct": round(raw_return, 4) if raw_return is not None else None,
        "decision_return_pct": round(decision_return, 4) if decision_return is not None else None,
        "direction": direction,
    }


def build_decision_review_baseline(
    *,
    portfolio: dict[str, Any],
    positions: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    decision: dict[str, Any],
    order: dict[str, Any] | None,
    fx_payload: dict[str, Any] | None,
    news_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    portfolio_for_nav = {**portfolio, "positions": positions}
    nav = portfolio_nav_snapshot(portfolio_for_nav, {}, fx_payload)
    entry = {"decision": decision, "order": order or {}, "candidates": candidates}
    target = decision_target(entry)
    decision_price = _baseline_price_from_entry(entry, target["code"]) if target["code"] else {}
    impacts = [_num(signal.get("impact_score"), 0) for signal in news_signals if isinstance(signal, dict)]
    return {
        "schema_version": 1,
        "portfolio_baseline": nav,
        "decision_price": decision_price,
        "target": target,
        "news_baseline": {
            "signal_count": len(news_signals),
            "max_impact_score": round(max(impacts), 4) if impacts else 0,
            "top_signals": [
                {
                    "title": str(signal.get("title") or "")[:160],
                    "impact_score": _num(signal.get("impact_score"), 0),
                    "direction": signal.get("direction"),
                    "matched_codes": signal.get("matched_codes") or signal.get("normalized_tickers") or [],
                }
                for signal in news_signals[:5]
                if isinstance(signal, dict)
            ],
        },
        "horizons": [{"days": days, "status": "pending"} for days in HORIZON_DAYS],
    }


def _load_followups() -> dict[str, Any]:
    if not FOLLOWUPS_PATH.exists():
        return {"schema_version": 1, "measurements": {}}
    try:
        payload = json.loads(FOLLOWUPS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "measurements": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "measurements": {}}
    measurements = payload.get("measurements")
    if not isinstance(measurements, dict):
        payload["measurements"] = {}
    payload.setdefault("schema_version", 1)
    return payload


def _save_followups(payload: dict[str, Any]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    FOLLOWUPS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_nav_history() -> dict[str, Any]:
    if not NAV_HISTORY_PATH.exists():
        return {"schema_version": 1, "snapshots": {}}
    try:
        payload = json.loads(NAV_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "snapshots": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "snapshots": {}}
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, dict):
        payload["snapshots"] = {}
    payload.setdefault("schema_version", 1)
    return payload


def _save_nav_history(payload: dict[str, Any]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    NAV_HISTORY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _history_bucket(timestamp: str) -> str:
    dt = _parse_dt(timestamp)
    if not dt:
        return str(timestamp or "")
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def update_nav_history(
    portfolio_navs: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    store = _load_nav_history()
    snapshots = store.setdefault("snapshots", {})
    timestamp = _iso(now)
    changed = False
    for row in portfolio_navs:
        portfolio_id = str(row.get("id") or "")
        nav = row.get("nav") if isinstance(row.get("nav"), dict) else {}
        nav_hkd = _num(nav.get("nav_hkd"), 0)
        if not portfolio_id or nav_hkd <= 0:
            continue
        point = {
            "timestamp": timestamp,
            "portfolio_id": portfolio_id,
            "portfolio_name": str(row.get("name") or ""),
            "nav_hkd": round(nav_hkd, 4),
            "cash_hkd": round(_num(nav.get("cash_hkd"), 0), 4),
            "market_value_hkd": round(_num(nav.get("market_value_hkd"), 0), 4),
            "unrealized_pnl_hkd": round(_num(nav.get("unrealized_pnl_hkd"), 0), 4),
            "estimated": bool(nav.get("estimated")),
            "source": "valuation_snapshot",
        }
        rows = [item for item in snapshots.get(portfolio_id, []) if isinstance(item, dict)]
        bucket = _history_bucket(timestamp)
        rows = [item for item in rows if _history_bucket(str(item.get("timestamp") or "")) != bucket]
        rows.append(point)
        rows.sort(key=lambda item: str(item.get("timestamp") or ""))
        snapshots[portfolio_id] = rows[-NAV_HISTORY_MAX_POINTS:]
        changed = True
    if changed:
        _save_nav_history(store)
    return store


def _measurement_from_quote(
    entry: dict[str, Any],
    code: str,
    baseline: dict[str, Any],
    quote_by_code: dict[str, dict[str, Any]],
    *,
    days: int,
    due_at: datetime,
    now: datetime,
) -> dict[str, Any] | None:
    baseline_price = _num(baseline.get("price"), 0)
    measured_price = quote_price(quote_by_code.get(code))
    if not code or baseline_price <= 0 or measured_price <= 0:
        return None
    returns = _return_payload(entry, baseline_price, measured_price)
    return {
        "days": days,
        "code": code,
        "due_at": _iso(due_at),
        "measured_at": _iso(now),
        "baseline_price": round(baseline_price, 4),
        "measured_price": round(measured_price, 4),
        "currency": str(baseline.get("currency") or _currency_for_code(code)).upper(),
        "price_source": "Futu OpenD snapshot at first due check",
        "measurement_timing": "first_available_after_due",
        **returns,
    }


def update_due_followups(
    entries: list[dict[str, Any]],
    quote_by_code: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    store = _load_followups()
    measurements = store.setdefault("measurements", {})
    changed = False

    for entry in entries:
        decision_id = decision_evaluation_id(entry)
        ts = _parse_dt(entry.get("timestamp") or entry.get("ts"))
        if not decision_id or not ts:
            continue
        target = decision_target(entry)
        code = target["code"]
        baseline = _baseline_price_from_entry(entry, code) if code else {}
        decision_measurements = measurements.setdefault(decision_id, {})
        for days in HORIZON_DAYS:
            key = str(days)
            if key in decision_measurements:
                continue
            due_at = ts + timedelta(days=days)
            if now < due_at:
                continue
            measurement = _measurement_from_quote(
                entry,
                code,
                baseline,
                quote_by_code,
                days=days,
                due_at=due_at,
                now=now,
            )
            if measurement:
                decision_measurements[key] = measurement
                changed = True

    if changed:
        _save_followups(store)
    return store


def collect_evaluation_codes(portfolios: list[dict[str, Any]], entries: list[dict[str, Any]]) -> list[str]:
    codes: set[str] = set()
    for portfolio in portfolios:
        for position in portfolio.get("positions") or []:
            if isinstance(position, dict) and position.get("code"):
                codes.add(str(position.get("code")).upper())
    for entry in entries:
        target = decision_target(entry)
        if target["code"]:
            codes.add(target["code"])
        for candidate in entry.get("candidates") or []:
            if isinstance(candidate, dict) and candidate.get("code"):
                codes.add(str(candidate.get("code")).upper())
    return sorted(codes)


def _application_status(entry: dict[str, Any]) -> str:
    application = entry.get("application") if isinstance(entry.get("application"), dict) else {}
    if application.get("status"):
        return str(application.get("status"))
    return "not_applicable" if not entry.get("order") else "unknown"


def _decision_tracking_rows(
    entries: list[dict[str, Any]],
    quote_by_code: dict[str, dict[str, Any]],
    followups: dict[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    measurements = followups.get("measurements") if isinstance(followups.get("measurements"), dict) else {}
    for entry in entries:
        evaluation_id = decision_evaluation_id(entry)
        decision_id = str(entry.get("decision_id") or "")
        ts = _parse_dt(entry.get("timestamp") or entry.get("ts"))
        decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
        portfolio = entry.get("portfolio") if isinstance(entry.get("portfolio"), dict) else {}
        target = decision_target(entry)
        code = target["code"]
        baseline = _baseline_price_from_entry(entry, code) if code else {}
        baseline_price = _num(baseline.get("price"), 0)
        current_price = quote_price(quote_by_code.get(code)) if code else 0.0
        current_return = _return_payload(entry, baseline_price, current_price) if baseline_price > 0 and current_price > 0 else {}
        decision_measurements = measurements.get(evaluation_id, {}) if isinstance(measurements, dict) else {}
        horizons: list[dict[str, Any]] = []
        for days in HORIZON_DAYS:
            key = str(days)
            due_at = ts + timedelta(days=days) if ts else None
            existing = decision_measurements.get(key) if isinstance(decision_measurements, dict) else None
            if isinstance(existing, dict):
                horizons.append({"days": days, "status": "measured", **existing})
            elif not ts or not due_at:
                horizons.append({"days": days, "status": "missing_timestamp", "due_at": ""})
            elif now < due_at:
                horizons.append({"days": days, "status": "pending", "due_at": _iso(due_at)})
            elif not code or baseline_price <= 0:
                horizons.append({"days": days, "status": "missing_baseline", "due_at": _iso(due_at)})
            else:
                horizons.append({"days": days, "status": "missing_quote", "due_at": _iso(due_at)})

        review = entry.get("review") if isinstance(entry.get("review"), dict) else {}
        nav = review.get("portfolio_baseline") if isinstance(review.get("portfolio_baseline"), dict) else {}
        news_baseline = review.get("news_baseline") if isinstance(review.get("news_baseline"), dict) else {}
        top_news = news_baseline.get("top_signals") if isinstance(news_baseline.get("top_signals"), list) else []
        human_review = entry.get("human_review") if isinstance(entry.get("human_review"), dict) else {}
        rows.append(
            {
                "decision_id": decision_id,
                "evaluation_id": evaluation_id,
                "timestamp": entry.get("timestamp") or entry.get("ts") or "",
                "portfolio_id": str(portfolio.get("id") or ""),
                "portfolio_name": str(portfolio.get("name") or entry.get("source") or ""),
                "action": str(decision.get("action") or "UNKNOWN"),
                "rating": str(decision.get("rating") or ""),
                "position_action": str(decision.get("position_action") or ""),
                "confidence": _num(decision.get("confidence"), 0),
                "application_status": _application_status(entry),
                "code": code,
                "target_source": target["source"],
                "baseline_price": round(baseline_price, 4) if baseline_price > 0 else None,
                "baseline_price_source": baseline.get("source"),
                "current_price": round(current_price, 4) if current_price > 0 else None,
                "current_return_pct": current_return.get("raw_return_pct"),
                "current_decision_return_pct": current_return.get("decision_return_pct"),
                "direction": current_return.get("direction", _direction(entry)),
                "portfolio_nav_hkd": nav.get("nav_hkd"),
                "news_signal_count": int(_num(news_baseline.get("signal_count"), 0)),
                "news_max_impact_score": _num(news_baseline.get("max_impact_score"), 0),
                "top_news_titles": [
                    str(signal.get("title") or "")
                    for signal in top_news[:3]
                    if isinstance(signal, dict) and str(signal.get("title") or "").strip()
                ],
                "human_review": {
                    "label": str(human_review.get("label") or ""),
                    "label_text": _review_label(str(human_review.get("label") or "")),
                    "note": str(human_review.get("note") or ""),
                    "updated_at": str(human_review.get("updated_at") or ""),
                },
                "horizons": horizons,
            }
        )
    rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return rows


def _trade_source_label(source: str) -> str:
    return TRADE_SOURCE_LABELS.get(str(source or "").lower(), "其他来源")


def _new_trade_source_bucket(source: str) -> dict[str, Any]:
    source_key = str(source or "unknown").lower()
    return {
        "source": source_key,
        "label": _trade_source_label(source_key),
        "trade_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "closed_trade_count": 0,
        "winning_trade_count": 0,
        "losing_trade_count": 0,
        "decision_count": 0,
        "turnover_hkd": 0.0,
        "realized_pnl_hkd": 0.0,
        "gross_profit_hkd": 0.0,
        "gross_loss_hkd": 0.0,
        "last_trade_at": "",
        "_decision_ids": set(),
    }


def _finalize_trade_source_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    decision_ids = bucket.pop("_decision_ids", set())
    bucket["decision_count"] = len(decision_ids)
    bucket["turnover_hkd"] = round(_num(bucket.get("turnover_hkd"), 0), 4)
    bucket["realized_pnl_hkd"] = round(_num(bucket.get("realized_pnl_hkd"), 0), 4)
    bucket["gross_profit_hkd"] = round(_num(bucket.get("gross_profit_hkd"), 0), 4)
    bucket["gross_loss_hkd"] = round(_num(bucket.get("gross_loss_hkd"), 0), 4)
    trade_count = int(bucket.get("trade_count") or 0)
    closed_count = int(bucket.get("closed_trade_count") or 0)
    loss_abs = abs(bucket["gross_loss_hkd"])
    bucket["avg_turnover_hkd"] = round(bucket["turnover_hkd"] / trade_count, 4) if trade_count else 0.0
    bucket["win_rate"] = round(int(bucket.get("winning_trade_count") or 0) / closed_count * 100, 2) if closed_count else None
    bucket["profit_loss_ratio"] = round(bucket["gross_profit_hkd"] / loss_abs, 4) if loss_abs > 0 else None
    return bucket


def _source_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    source = str(row.get("source") or "")
    try:
        return (TRADE_SOURCE_ORDER.index(source), source)
    except ValueError:
        return (len(TRADE_SOURCE_ORDER), source)


def _accumulate_trade_bucket(bucket: dict[str, Any], trade: dict[str, Any], rates: dict[str, float]) -> tuple[float, float]:
    side = str(trade.get("side") or "").upper()
    currency = str(trade.get("currency") or _currency_for_code(str(trade.get("code") or ""))).upper()
    realized = _to_hkd(_num(trade.get("realized_pnl"), 0), currency, rates)
    turnover = _to_hkd(_num(trade.get("notional"), 0), currency, rates)
    bucket["trade_count"] += 1
    bucket["turnover_hkd"] += turnover
    bucket["realized_pnl_hkd"] += realized
    if side == "BUY":
        bucket["buy_count"] += 1
    elif side == "SELL":
        bucket["sell_count"] += 1
        bucket["closed_trade_count"] += 1
        if realized > 0:
            bucket["winning_trade_count"] += 1
            bucket["gross_profit_hkd"] += realized
        elif realized < 0:
            bucket["losing_trade_count"] += 1
            bucket["gross_loss_hkd"] += realized
    decision_id = str(trade.get("decision_id") or "").strip()
    if decision_id:
        bucket["_decision_ids"].add(decision_id)
    created_at = str(trade.get("created_at") or "")
    if created_at and created_at > str(bucket.get("last_trade_at") or ""):
        bucket["last_trade_at"] = created_at
    return realized, turnover


def _portfolio_trade_stats(portfolio: dict[str, Any], rates: dict[str, float]) -> dict[str, Any]:
    trades = [item for item in portfolio.get("trades") or [] if isinstance(item, dict)]
    by_source: dict[str, int] = {}
    sources: dict[str, dict[str, Any]] = {}
    realized_pnl_hkd = 0.0
    turnover_hkd = 0.0
    first_trade_at = ""
    last_trade_at = ""
    closed_trade_count = 0
    winning_trade_count = 0
    losing_trade_count = 0
    gross_profit_hkd = 0.0
    gross_loss_hkd = 0.0
    for trade in trades:
        source = str(trade.get("source") or "unknown").lower()
        by_source[source] = by_source.get(source, 0) + 1
        bucket = sources.setdefault(source, _new_trade_source_bucket(source))
        realized, turnover = _accumulate_trade_bucket(bucket, trade, rates)
        realized_pnl_hkd += realized
        turnover_hkd += turnover
        created_at = str(trade.get("created_at") or "")
        if created_at:
            first_trade_at = min(first_trade_at, created_at) if first_trade_at else created_at
            last_trade_at = max(last_trade_at, created_at) if last_trade_at else created_at
        if str(trade.get("side") or "").upper() == "SELL":
            closed_trade_count += 1
            if realized > 0:
                winning_trade_count += 1
                gross_profit_hkd += realized
            elif realized < 0:
                losing_trade_count += 1
                gross_loss_hkd += realized
    first_dt = _parse_dt(first_trade_at)
    last_dt = _parse_dt(last_trade_at)
    active_days = max(((last_dt - first_dt).total_seconds() / 86400), 1.0) if first_dt and last_dt else 0.0
    loss_abs = abs(gross_loss_hkd)
    return {
        "trade_count": len(trades),
        "by_source": by_source,
        "sources": sorted((_finalize_trade_source_bucket(bucket) for bucket in sources.values()), key=_source_sort_key),
        "realized_pnl_hkd": round(realized_pnl_hkd, 4),
        "turnover_hkd": round(turnover_hkd, 4),
        "closed_trade_count": closed_trade_count,
        "winning_trade_count": winning_trade_count,
        "losing_trade_count": losing_trade_count,
        "win_rate": round(winning_trade_count / closed_trade_count * 100, 2) if closed_trade_count else None,
        "profit_loss_ratio": round(gross_profit_hkd / loss_abs, 4) if loss_abs > 0 else None,
        "gross_profit_hkd": round(gross_profit_hkd, 4),
        "gross_loss_hkd": round(gross_loss_hkd, 4),
        "first_trade_at": first_trade_at,
        "last_trade_at": last_trade_at,
        "trades_per_day": round(len(trades) / active_days, 4) if active_days > 0 else 0.0,
    }


def _trade_attribution(portfolios: list[dict[str, Any]], rates: dict[str, float]) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    portfolio_ids_by_source: dict[str, set[str]] = {}
    total_trade_count = 0
    total_turnover_hkd = 0.0
    total_realized_pnl_hkd = 0.0
    for portfolio in portfolios:
        portfolio_id = str(portfolio.get("id") or "")
        for trade in [item for item in portfolio.get("trades") or [] if isinstance(item, dict)]:
            source = str(trade.get("source") or "unknown").lower()
            bucket = buckets.setdefault(source, _new_trade_source_bucket(source))
            portfolio_ids_by_source.setdefault(source, set()).add(portfolio_id)
            realized, turnover = _accumulate_trade_bucket(bucket, trade, rates)
            total_trade_count += 1
            total_turnover_hkd += turnover
            total_realized_pnl_hkd += realized

    rows = []
    for source, bucket in buckets.items():
        row = _finalize_trade_source_bucket(bucket)
        row["portfolio_count"] = len(portfolio_ids_by_source.get(source, set()))
        rows.append(row)
    return {
        "trade_count": total_trade_count,
        "source_count": len(rows),
        "turnover_hkd": round(total_turnover_hkd, 4),
        "realized_pnl_hkd": round(total_realized_pnl_hkd, 4),
        "sources": sorted(rows, key=_source_sort_key),
    }


def _sync_status_bucket(status: str) -> str:
    normalized = str(status or "").lower()
    if normalized in {"applied", "filled"}:
        return "filled"
    if normalized in {"partial", "partially_applied"}:
        return "partial"
    if normalized in {"submitted", "futu_submitted", "no_new_fill"}:
        return "pending"
    if normalized in {"futu_submit_failed", "local_apply_failed", "failed"}:
        return "failed"
    return "unknown"


def _new_execution_summary(portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "order_count": 0,
        "filled_count": 0,
        "partial_count": 0,
        "pending_count": 0,
        "failed_count": 0,
        "unknown_count": 0,
        "intended_notional_hkd": 0.0,
        "filled_notional_hkd": 0.0,
        "total_qty": 0.0,
        "dealt_qty": 0.0,
        "measured_slippage_count": 0,
        "weighted_slippage_pct_sum": 0.0,
        "slippage_weight_hkd": 0.0,
        "adverse_slippage_hkd": 0.0,
        "status_counts": {},
    }
    if portfolio:
        row.update({"portfolio_id": str(portfolio.get("id") or ""), "portfolio_name": str(portfolio.get("name") or "")})
    return row


def _accumulate_execution_summary(summary: dict[str, Any], order: dict[str, Any], rates: dict[str, float]) -> dict[str, Any]:
    code = str(order.get("code") or "").upper()
    side = str(order.get("side") or "").upper()
    status = str(order.get("status") or "unknown").lower()
    bucket = _sync_status_bucket(status)
    qty = _num(order.get("qty"), 0)
    dealt_qty = _num(order.get("dealt_qty"), 0)
    plan_price = _num(order.get("price"), 0)
    avg_price = _num(order.get("dealt_avg_price"), 0)
    currency = _currency_for_code(code)
    rate = _num(rates.get(currency), 0)
    intended_notional_hkd = qty * plan_price * rate if qty > 0 and plan_price > 0 and rate > 0 else 0.0
    filled_notional_hkd = dealt_qty * avg_price * rate if dealt_qty > 0 and avg_price > 0 and rate > 0 else 0.0

    summary["order_count"] += 1
    summary[f"{bucket}_count"] = int(summary.get(f"{bucket}_count", 0)) + 1
    summary["intended_notional_hkd"] += intended_notional_hkd
    summary["filled_notional_hkd"] += filled_notional_hkd
    summary["total_qty"] += qty
    summary["dealt_qty"] += dealt_qty
    status_counts = summary.setdefault("status_counts", {})
    status_counts[status] = status_counts.get(status, 0) + 1

    slippage_pct = None
    slippage_hkd = None
    if plan_price > 0 and avg_price > 0 and dealt_qty > 0 and rate > 0:
        if side == "SELL":
            slippage_pct = (plan_price - avg_price) / plan_price * 100
            slippage_hkd = (plan_price - avg_price) * dealt_qty * rate
        else:
            slippage_pct = (avg_price - plan_price) / plan_price * 100
            slippage_hkd = (avg_price - plan_price) * dealt_qty * rate
        weight = filled_notional_hkd or abs(slippage_hkd)
        summary["measured_slippage_count"] += 1
        summary["weighted_slippage_pct_sum"] += slippage_pct * weight
        summary["slippage_weight_hkd"] += weight
        summary["adverse_slippage_hkd"] += slippage_hkd

    fill_rate_pct = round(dealt_qty / qty * 100, 4) if qty > 0 else None
    return {
        "portfolio_id": str(order.get("portfolio_id") or ""),
        "order_id": str(order.get("order_id") or order.get("id") or ""),
        "decision_id": str(order.get("decision_id") or ""),
        "source": str(order.get("source") or ""),
        "code": code,
        "side": side,
        "status": status,
        "status_bucket": bucket,
        "qty": round(qty, 4),
        "dealt_qty": round(dealt_qty, 4),
        "fill_rate_pct": fill_rate_pct,
        "plan_price": round(plan_price, 4) if plan_price > 0 else None,
        "dealt_avg_price": round(avg_price, 4) if avg_price > 0 else None,
        "currency": currency,
        "intended_notional_hkd": round(intended_notional_hkd, 4),
        "filled_notional_hkd": round(filled_notional_hkd, 4),
        "adverse_slippage_pct": round(slippage_pct, 4) if slippage_pct is not None else None,
        "adverse_slippage_hkd": round(slippage_hkd, 4) if slippage_hkd is not None else None,
        "message": str(order.get("message") or ""),
        "created_at": str(order.get("created_at") or ""),
        "updated_at": str(order.get("updated_at") or order.get("created_at") or ""),
    }


def _finalize_execution_summary(summary: dict[str, Any]) -> dict[str, Any]:
    order_count = int(summary.get("order_count") or 0)
    total_qty = _num(summary.get("total_qty"), 0)
    weight = _num(summary.get("slippage_weight_hkd"), 0)
    intended = _num(summary.get("intended_notional_hkd"), 0)
    for key in ("intended_notional_hkd", "filled_notional_hkd", "adverse_slippage_hkd"):
        summary[key] = round(_num(summary.get(key), 0), 4)
    summary["fill_rate_pct"] = round(_num(summary.get("dealt_qty"), 0) / total_qty * 100, 2) if total_qty > 0 else None
    summary["failure_rate_pct"] = round(int(summary.get("failed_count") or 0) / order_count * 100, 2) if order_count else None
    summary["partial_rate_pct"] = round(int(summary.get("partial_count") or 0) / order_count * 100, 2) if order_count else None
    summary["avg_adverse_slippage_pct"] = round(_num(summary.get("weighted_slippage_pct_sum"), 0) / weight, 4) if weight > 0 else None
    summary["slippage_to_intended_pct"] = round(summary["adverse_slippage_hkd"] / intended * 100, 4) if intended > 0 else None
    for private_key in ("weighted_slippage_pct_sum", "slippage_weight_hkd", "total_qty", "dealt_qty"):
        summary.pop(private_key, None)
    return summary


def _futu_execution_quality(portfolios: list[dict[str, Any]], rates: dict[str, float]) -> dict[str, Any]:
    total = _new_execution_summary()
    portfolio_rows: list[dict[str, Any]] = []
    recent_orders: list[dict[str, Any]] = []
    for portfolio in portfolios:
        portfolio_summary = _new_execution_summary(portfolio)
        for raw_order in [item for item in portfolio.get("futu_sync_orders") or [] if isinstance(item, dict)]:
            order = dict(raw_order)
            order["portfolio_id"] = str(portfolio.get("id") or "")
            row = _accumulate_execution_summary(portfolio_summary, order, rates)
            _accumulate_execution_summary(total, order, rates)
            row["portfolio_name"] = str(portfolio.get("name") or "")
            recent_orders.append(row)
        if portfolio_summary["order_count"]:
            portfolio_rows.append(_finalize_execution_summary(portfolio_summary))
    recent_orders.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return {
        "summary": _finalize_execution_summary(total),
        "portfolios": portfolio_rows,
        "recent_orders": recent_orders[:80],
    }


def _curve_stats(points: list[dict[str, Any]]) -> dict[str, Any]:
    navs = [_num(point.get("nav_hkd"), 0) for point in points if _num(point.get("nav_hkd"), 0) > 0]
    if not navs:
        return {"return_pct": None, "max_drawdown_pct": None}
    first = navs[0]
    last = navs[-1]
    peak = navs[0]
    max_drawdown = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            max_drawdown = min(max_drawdown, (nav - peak) / peak * 100)
    return {
        "return_pct": round((last - first) / first * 100, 4) if first > 0 else None,
        "max_drawdown_pct": round(max_drawdown, 4),
    }


def _equity_curve_for_portfolio(
    portfolio: dict[str, Any],
    entries: list[dict[str, Any]],
    current_nav: dict[str, Any],
    nav_history: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    portfolio_id = str(portfolio.get("id") or "")
    points: list[dict[str, Any]] = []
    snapshots = nav_history.get("snapshots") if isinstance(nav_history.get("snapshots"), dict) else {}
    for history_point in snapshots.get(portfolio_id, []):
        if not isinstance(history_point, dict):
            continue
        nav = _num(history_point.get("nav_hkd"), 0)
        timestamp = str(history_point.get("timestamp") or "")
        if nav <= 0 or not timestamp:
            continue
        points.append(
            {
                "timestamp": timestamp,
                "nav_hkd": round(nav, 4),
                "source": str(history_point.get("source") or "valuation_snapshot"),
                "estimated": bool(history_point.get("estimated")),
            }
        )
    for entry in entries:
        entry_portfolio = entry.get("portfolio") if isinstance(entry.get("portfolio"), dict) else {}
        if str(entry_portfolio.get("id") or "") != portfolio_id:
            continue
        review = entry.get("review") if isinstance(entry.get("review"), dict) else {}
        baseline = review.get("portfolio_baseline") if isinstance(review.get("portfolio_baseline"), dict) else {}
        nav = _num(baseline.get("nav_hkd"), 0)
        if nav <= 0:
            continue
        points.append(
            {
                "timestamp": entry.get("timestamp") or entry.get("ts") or "",
                "nav_hkd": round(nav, 4),
                "source": "decision_baseline",
                "estimated": bool(baseline.get("estimated")),
            }
        )
    current_timestamp = _iso(now)
    has_current = any(str(point.get("timestamp") or "") == current_timestamp for point in points)
    if not has_current and _num(current_nav.get("nav_hkd"), 0) > 0:
        points.append(
            {
                "timestamp": current_timestamp,
                "nav_hkd": current_nav.get("nav_hkd"),
                "source": "current_mark_to_market",
                "estimated": bool(current_nav.get("estimated")),
            }
        )
    points.sort(key=lambda item: str(item.get("timestamp") or ""))
    if points:
        first = _num(points[0].get("nav_hkd"), 0)
        for point in points:
            nav = _num(point.get("nav_hkd"), 0)
            point["return_pct"] = round((nav - first) / first * 100, 4) if first > 0 else None
    return {"portfolio_id": portfolio_id, "points": points, "stats": _curve_stats(points)}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    denom_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if denom_x <= 0 or denom_y <= 0:
        return None
    return round(numerator / (denom_x * denom_y), 4)


def _decision_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    measured = []
    directional = []
    confidence_xs: list[float] = []
    return_ys: list[float] = []
    for row in rows:
        for horizon in row.get("horizons") or []:
            if horizon.get("status") != "measured":
                continue
            measured.append(horizon)
            if horizon.get("decision_return_pct") is not None:
                directional.append(horizon)
                confidence_xs.append(_num(row.get("confidence"), 0))
                return_ys.append(_num(horizon.get("decision_return_pct"), 0))
    wins = [item for item in directional if _num(item.get("decision_return_pct"), 0) > 0]
    avg = sum(_num(item.get("decision_return_pct"), 0) for item in directional) / len(directional) if directional else 0.0
    return {
        "decision_count": len(rows),
        "measured_horizons": len(measured),
        "directional_horizons": len(directional),
        "win_count": len(wins),
        "win_rate": round(len(wins) / len(directional) * 100, 2) if directional else None,
        "avg_decision_return_pct": round(avg, 4) if directional else None,
        "confidence_return_correlation": _pearson(confidence_xs, return_ys),
    }


def _review_label(label: str) -> str:
    return REVIEW_LABELS.get(str(label or ""), "未复盘")


def _review_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    reviewed = 0
    for row in rows:
        review = row.get("human_review") if isinstance(row.get("human_review"), dict) else {}
        label = str(review.get("label") or "")
        if not label:
            continue
        reviewed += 1
        counts[label] = counts.get(label, 0) + 1
    return {
        "reviewed_count": reviewed,
        "unreviewed_count": max(len(rows) - reviewed, 0),
        "label_counts": [
            {"label": label, "label_text": _review_label(label), "count": count}
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def _bucket_decision_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    directional = []
    current_values = []
    for row in rows:
        current = row.get("current_decision_return_pct")
        if current is not None:
            current_values.append(_num(current, 0))
        for horizon in row.get("horizons") or []:
            if horizon.get("status") == "measured" and horizon.get("decision_return_pct") is not None:
                directional.append(_num(horizon.get("decision_return_pct"), 0))
    wins = [value for value in directional if value > 0]
    return {
        "decision_count": len(rows),
        "measured_directional_count": len(directional),
        "win_rate": round(len(wins) / len(directional) * 100, 2) if directional else None,
        "avg_decision_return_pct": round(sum(directional) / len(directional), 4) if directional else None,
        "avg_current_decision_return_pct": round(sum(current_values) / len(current_values), 4) if current_values else None,
    }


def _news_effect_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = {
        "news_driven": {"label": "新闻驱动", "rows": []},
        "high_impact": {"label": "高影响新闻", "rows": []},
        "no_news": {"label": "无新闻基线", "rows": []},
    }
    for row in rows:
        signal_count = int(row.get("news_signal_count") or 0)
        max_impact = _num(row.get("news_max_impact_score"), 0)
        if signal_count > 0:
            buckets["news_driven"]["rows"].append(row)
            if max_impact >= 70:
                buckets["high_impact"]["rows"].append(row)
        else:
            buckets["no_news"]["rows"].append(row)
    return {
        "buckets": [
            {"key": key, "label": payload["label"], **_bucket_decision_stats(payload["rows"])}
            for key, payload in buckets.items()
        ]
    }


def _ab_test_summary(
    portfolios: list[dict[str, Any]],
    portfolio_summaries: list[dict[str, Any]],
    equity_curves: list[dict[str, Any]],
    tracking_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    curve_by_id = {str(curve.get("portfolio_id") or ""): curve for curve in equity_curves}
    decisions_by_portfolio: dict[str, list[dict[str, Any]]] = {}
    for row in tracking_rows:
        decisions_by_portfolio.setdefault(str(row.get("portfolio_id") or ""), []).append(row)
    raw_by_id = {str(portfolio.get("id") or ""): portfolio for portfolio in portfolios}
    rows: list[dict[str, Any]] = []
    for summary in portfolio_summaries:
        portfolio_id = str(summary.get("id") or "")
        raw = raw_by_id.get(portfolio_id, {})
        curve = curve_by_id.get(portfolio_id, {})
        points = curve.get("points") or []
        first_nav = _num((points[0] if points else {}).get("nav_hkd"), 0)
        nav = summary.get("nav") if isinstance(summary.get("nav"), dict) else {}
        trade_stats = summary.get("trade_stats") if isinstance(summary.get("trade_stats"), dict) else {}
        decision_stats = _bucket_decision_stats(decisions_by_portfolio.get(portfolio_id, []))
        rows.append(
            {
                "portfolio_id": portfolio_id,
                "portfolio_name": str(summary.get("name") or ""),
                "parent_id": str(raw.get("parent_id") or ""),
                "portfolio_kind": str(summary.get("portfolio_kind") or "paper"),
                "apply_mode": str(summary.get("apply_mode") or "manual"),
                "strategy_profile": str(summary.get("strategy_profile") or ""),
                "strategy_tags": list(summary.get("strategy_tags") or []),
                "initial_nav_hkd": round(first_nav, 4) if first_nav > 0 else None,
                "nav_hkd": nav.get("nav_hkd"),
                "return_pct": (curve.get("stats") or {}).get("return_pct"),
                "max_drawdown_pct": (curve.get("stats") or {}).get("max_drawdown_pct"),
                "trade_count": trade_stats.get("trade_count", 0),
                "trades_per_day": trade_stats.get("trades_per_day", 0),
                "turnover_hkd": trade_stats.get("turnover_hkd", 0),
                "trade_win_rate": trade_stats.get("win_rate"),
                "profit_loss_ratio": trade_stats.get("profit_loss_ratio"),
                "decision_count": decision_stats.get("decision_count", 0),
                "decision_win_rate": decision_stats.get("win_rate"),
                "avg_decision_return_pct": decision_stats.get("avg_decision_return_pct"),
            }
        )
    group_map: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        root = str(row.get("parent_id") or row.get("portfolio_id") or "")
        group_map.setdefault(root, []).append(row)
    groups = [
        {
            "root_portfolio_id": root,
            "root_portfolio_name": str(raw_by_id.get(root, {}).get("name") or root),
            "members": members,
        }
        for root, members in group_map.items()
        if len(members) > 1
    ]
    return {"rows": rows, "groups": groups}


def _strategy_breakdown(
    portfolio_summaries: list[dict[str, Any]],
    tracking_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    portfolio_by_id = {str(row.get("id") or ""): row for row in portfolio_summaries}
    buckets: dict[str, dict[str, Any]] = {}
    for portfolio in portfolio_summaries:
        key = str(portfolio.get("strategy_profile") or portfolio.get("apply_mode") or "general")
        bucket = buckets.setdefault(
            key,
            {
                "key": key,
                "label": key,
                "portfolio_count": 0,
                "trade_count": 0,
                "turnover_hkd": 0.0,
                "realized_pnl_hkd": 0.0,
                "tags": set(),
                "decisions": [],
            },
        )
        bucket["portfolio_count"] += 1
        stats = portfolio.get("trade_stats") if isinstance(portfolio.get("trade_stats"), dict) else {}
        bucket["trade_count"] += int(stats.get("trade_count") or 0)
        bucket["turnover_hkd"] += _num(stats.get("turnover_hkd"), 0)
        bucket["realized_pnl_hkd"] += _num(stats.get("realized_pnl_hkd"), 0)
        bucket["tags"].update(str(tag) for tag in portfolio.get("strategy_tags") or [] if str(tag).strip())
    for row in tracking_rows:
        portfolio = portfolio_by_id.get(str(row.get("portfolio_id") or ""), {})
        key = str(portfolio.get("strategy_profile") or portfolio.get("apply_mode") or "general")
        buckets.setdefault(
            key,
            {
                "key": key,
                "label": key,
                "portfolio_count": 0,
                "trade_count": 0,
                "turnover_hkd": 0.0,
                "realized_pnl_hkd": 0.0,
                "tags": set(),
                "decisions": [],
            },
        )["decisions"].append(row)
    rows = []
    for bucket in buckets.values():
        stats = _bucket_decision_stats(bucket.pop("decisions", []))
        tags = sorted(bucket.pop("tags", set()))
        rows.append(
            {
                **bucket,
                "tags": tags,
                "turnover_hkd": round(_num(bucket.get("turnover_hkd"), 0), 4),
                "realized_pnl_hkd": round(_num(bucket.get("realized_pnl_hkd"), 0), 4),
                **stats,
            }
        )
    rows.sort(key=lambda item: (str(item.get("key") or "")))
    return {"rows": rows}


def build_evaluation_payload(
    *,
    portfolios: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    quote_by_code: dict[str, dict[str, Any]],
    fx_payload: dict[str, Any],
    quote_error: str = "",
    portfolio_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected_portfolio_id = str(portfolio_id or "").strip()
    filtered_portfolios = [
        portfolio for portfolio in portfolios if not selected_portfolio_id or str(portfolio.get("id") or "") == selected_portfolio_id
    ]
    filtered_entries = []
    for entry in entries:
        entry_portfolio = entry.get("portfolio") if isinstance(entry.get("portfolio"), dict) else {}
        if selected_portfolio_id and str(entry_portfolio.get("id") or "") != selected_portfolio_id:
            continue
        filtered_entries.append(entry)

    followups = update_due_followups(filtered_entries, quote_by_code, now=now)
    rates = _rates_to_hkd(fx_payload)
    portfolio_summaries: list[dict[str, Any]] = []
    equity_curves: list[dict[str, Any]] = []
    for portfolio in filtered_portfolios:
        nav = portfolio_nav_snapshot(portfolio, quote_by_code, fx_payload)
        trade_stats = _portfolio_trade_stats(portfolio, rates)
        portfolio_summaries.append(
            {
                "id": str(portfolio.get("id") or ""),
                "name": str(portfolio.get("name") or ""),
                "portfolio_kind": str(portfolio.get("portfolio_kind") or "paper"),
                "apply_mode": str(portfolio.get("apply_mode") or "manual"),
                "parent_id": str(portfolio.get("parent_id") or ""),
                "strategy_profile": str(portfolio.get("strategy_profile") or ""),
                "strategy_tags": list(portfolio.get("strategy_tags") or []),
                "nav": nav,
                "trade_stats": trade_stats,
            }
        )

    nav_history = update_nav_history(portfolio_summaries, now=now)
    for portfolio in filtered_portfolios:
        current_nav = next((row.get("nav", {}) for row in portfolio_summaries if row.get("id") == str(portfolio.get("id") or "")), {})
        equity_curves.append(_equity_curve_for_portfolio(portfolio, filtered_entries, current_nav, nav_history, now=now))

    tracking_rows = _decision_tracking_rows(filtered_entries, quote_by_code, followups, now=now)
    metrics = _decision_metrics(tracking_rows)
    return {
        "ok": not quote_error,
        "as_of": _iso(now),
        "quote_error": quote_error,
        "fx": fx_payload,
        "portfolio_summaries": portfolio_summaries,
        "equity_curves": equity_curves,
        "decision_tracking": tracking_rows,
        "metrics": metrics,
        "attribution": _trade_attribution(filtered_portfolios, rates),
        "execution_quality": _futu_execution_quality(filtered_portfolios, rates),
        "ab_tests": _ab_test_summary(filtered_portfolios, portfolio_summaries, equity_curves, tracking_rows),
        "strategy_breakdown": _strategy_breakdown(portfolio_summaries, tracking_rows),
        "news_effect": _news_effect_summary(tracking_rows),
        "review_summary": _review_summary(tracking_rows),
        "nav_history_updated_at": nav_history.get("updated_at", ""),
        "followups_updated_at": followups.get("updated_at", ""),
    }
