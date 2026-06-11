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
HORIZON_DAYS = (1, 3, 7)


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
                "horizons": horizons,
            }
        )
    rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return rows


def _decision_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    measured = []
    directional = []
    for row in rows:
        for horizon in row.get("horizons") or []:
            if horizon.get("status") != "measured":
                continue
            measured.append(horizon)
            if horizon.get("decision_return_pct") is not None:
                directional.append(horizon)
    wins = [item for item in directional if _num(item.get("decision_return_pct"), 0) > 0]
    avg = sum(_num(item.get("decision_return_pct"), 0) for item in directional) / len(directional) if directional else 0.0
    return {
        "decision_count": len(rows),
        "measured_horizons": len(measured),
        "directional_horizons": len(directional),
        "win_count": len(wins),
        "win_rate": round(len(wins) / len(directional) * 100, 2) if directional else None,
        "avg_decision_return_pct": round(avg, 4) if directional else None,
    }


def _portfolio_trade_stats(portfolio: dict[str, Any], rates: dict[str, float]) -> dict[str, Any]:
    trades = [item for item in portfolio.get("trades") or [] if isinstance(item, dict)]
    by_source: dict[str, int] = {}
    realized_pnl_hkd = 0.0
    turnover_hkd = 0.0
    for trade in trades:
        source = str(trade.get("source") or "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        currency = str(trade.get("currency") or _currency_for_code(str(trade.get("code") or ""))).upper()
        realized_pnl_hkd += _to_hkd(_num(trade.get("realized_pnl"), 0), currency, rates)
        turnover_hkd += _to_hkd(_num(trade.get("notional"), 0), currency, rates)
    return {
        "trade_count": len(trades),
        "by_source": by_source,
        "realized_pnl_hkd": round(realized_pnl_hkd, 4),
        "turnover_hkd": round(turnover_hkd, 4),
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
) -> dict[str, Any]:
    portfolio_id = str(portfolio.get("id") or "")
    points: list[dict[str, Any]] = []
    for entry in entries:
        entry_portfolio = entry.get("portfolio") if isinstance(entry.get("portfolio"), dict) else {}
        if str(entry_portfolio.get("id") or "") != portfolio_id:
            continue
        review = entry.get("review") if isinstance(entry.get("review"), dict) else {}
        baseline = review.get("portfolio_baseline") if isinstance(review.get("portfolio_baseline"), dict) else {}
        nav = _num(baseline.get("nav_hkd"), 0)
        estimated = bool(baseline.get("estimated"))
        if nav <= 0:
            cash = _num(entry_portfolio.get("cash"), 0)
            currency = str(entry_portfolio.get("base_currency") or portfolio.get("base_currency") or "HKD").upper()
            nav = _to_hkd(cash, currency, _rates_to_hkd(entry_portfolio or portfolio))
            estimated = True
        if nav > 0:
            points.append(
                {
                    "timestamp": entry.get("timestamp") or entry.get("ts") or "",
                    "nav_hkd": round(nav, 4),
                    "source": "decision_baseline" if baseline else "decision_cash_fallback",
                    "estimated": estimated,
                }
            )
    if _num(current_nav.get("nav_hkd"), 0) > 0:
        points.append(
            {
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
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
                "nav": nav,
                "trade_stats": trade_stats,
            }
        )
        equity_curves.append(_equity_curve_for_portfolio(portfolio, filtered_entries, nav))

    tracking_rows = _decision_tracking_rows(filtered_entries, quote_by_code, followups, now=now)
    return {
        "ok": not quote_error,
        "as_of": _iso(now),
        "quote_error": quote_error,
        "fx": fx_payload,
        "portfolio_summaries": portfolio_summaries,
        "equity_curves": equity_curves,
        "decision_tracking": tracking_rows,
        "metrics": _decision_metrics(tracking_rows),
        "followups_updated_at": followups.get("updated_at", ""),
    }
