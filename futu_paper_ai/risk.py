from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .config import RiskConfig
from .models import OrderIntent


DEFAULT_FX_TO_HKD = {"HKD": 1.0, "USD": 7.8, "CNY": 1.08, "CNH": 1.08}


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    violations: list[str]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _string_set(value: Any, default: set[str]) -> set[str]:
    if isinstance(value, str):
        raw_items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        return set(default)
    items = {str(item or "").strip().upper() for item in raw_items if str(item or "").strip()}
    return items if items else set(default)


def _float_map_override(value: Any, default: dict[str, float]) -> dict[str, float]:
    if not isinstance(value, dict):
        return dict(default)
    result = dict(default)
    for market, raw in value.items():
        key = str(market or "").strip().upper()
        if key not in {"US", "HK", "CN"}:
            continue
        number = _num(raw, -1)
        if number >= 0:
            result[key] = number
    return result


def risk_config_with_overrides(config: RiskConfig, overrides: dict[str, Any] | None) -> RiskConfig:
    """Return a portfolio-specific risk config without mutating the global one."""
    if not isinstance(overrides, dict) or not overrides:
        return config

    values: dict[str, Any] = {}
    if "allowed_markets" in overrides:
        values["allowed_markets"] = _string_set(overrides.get("allowed_markets"), config.allowed_markets)
    if "allowed_codes" in overrides:
        values["allowed_codes"] = _string_set(overrides.get("allowed_codes"), config.allowed_codes)
    for key in ("require_whitelist", "allow_sell", "allow_market_orders"):
        if key in overrides:
            values[key] = _bool(overrides.get(key), getattr(config, key))
    for key in ("max_order_value", "max_qty"):
        if key in overrides:
            values[key] = _float_map_override(overrides.get(key), getattr(config, key))
    for key in ("max_position_pct", "max_equity_exposure_pct", "min_cash_pct"):
        if key in overrides:
            values[key] = max(0.0, _num(overrides.get(key), getattr(config, key)))
    return replace(config, **values)


def risk_config_to_payload(config: RiskConfig) -> dict[str, Any]:
    return {
        "allowed_markets": sorted(config.allowed_markets),
        "allowed_codes": sorted(config.allowed_codes),
        "require_whitelist": config.require_whitelist,
        "allow_sell": config.allow_sell,
        "allow_market_orders": config.allow_market_orders,
        "max_order_value": dict(config.max_order_value),
        "max_qty": dict(config.max_qty),
        "max_position_pct": config.max_position_pct,
        "max_equity_exposure_pct": config.max_equity_exposure_pct,
        "min_cash_pct": config.min_cash_pct,
    }


def _currency_for_market(market: str) -> str:
    return {"US": "USD", "HK": "HKD", "CN": "CNY"}.get(market.upper(), "USD")


def _rates_to_hkd(portfolio: dict[str, Any], fx_to_hkd: dict[str, Any] | None = None) -> dict[str, float]:
    rates = dict(DEFAULT_FX_TO_HKD)
    for raw in (portfolio.get("fx_to_hkd"), fx_to_hkd):
        if not isinstance(raw, dict):
            continue
        for currency, value in raw.items():
            code = str(currency or "").strip().upper()
            rate = _num(value, 0)
            if code and rate > 0:
                rates[code] = rate
    return rates


def _to_hkd(amount: float, currency: str, rates: dict[str, float]) -> float:
    return amount * _num(rates.get(str(currency or "").upper()), 0)


def _position_price(position: dict[str, Any]) -> float:
    for key in ("last_price", "market_price", "price", "cost_price"):
        price = _num(position.get(key), 0)
        if price > 0:
            return price
    return 0.0


def portfolio_risk_violations(
    intent: OrderIntent,
    portfolio: dict[str, Any],
    config: RiskConfig,
    *,
    positions: list[dict[str, Any]] | None = None,
    fx_to_hkd: dict[str, Any] | None = None,
) -> list[str]:
    if intent.side != "BUY":
        return []

    rates = _rates_to_hkd(portfolio, fx_to_hkd)
    base_currency = str(portfolio.get("base_currency") or "HKD").upper()
    cash_payload = portfolio.get("cash_by_currency")
    if not isinstance(cash_payload, dict):
        cash_payload = {base_currency: portfolio.get("cash", 0)}

    cash_hkd = sum(_to_hkd(_num(amount), str(currency), rates) for currency, amount in cash_payload.items())
    position_rows = positions if positions is not None else portfolio.get("positions") or []
    equity_by_code_hkd: dict[str, float] = {}
    for position in position_rows:
        if not isinstance(position, dict):
            continue
        code = str(position.get("code") or "").upper()
        qty = _num(position.get("qty"), 0)
        price = _position_price(position)
        currency = str(position.get("currency") or _currency_for_market(intent.market)).upper()
        if not code or qty <= 0 or price <= 0:
            continue
        equity_by_code_hkd[code] = equity_by_code_hkd.get(code, 0.0) + _to_hkd(qty * price, currency, rates)

    current_equity_hkd = sum(equity_by_code_hkd.values())
    nav_hkd = cash_hkd + current_equity_hkd
    if nav_hkd <= 0:
        return []

    currency = _currency_for_market(intent.market)
    order_hkd = _to_hkd(intent.notional, currency, rates)
    post_cash_hkd = cash_hkd - order_hkd
    post_equity_hkd = current_equity_hkd + order_hkd
    post_code_hkd = equity_by_code_hkd.get(intent.code, 0.0) + order_hkd

    violations: list[str] = []
    max_position_pct = _num(config.max_position_pct, 0)
    if max_position_pct > 0:
        position_pct = post_code_hkd / nav_hkd * 100
        if position_pct > max_position_pct:
            violations.append(
                f"portfolio concentration {position_pct:.2f}% exceeds max_position_pct {max_position_pct:.2f}%"
            )

    max_equity_pct = _num(config.max_equity_exposure_pct, 0)
    if max_equity_pct > 0:
        equity_pct = post_equity_hkd / nav_hkd * 100
        if equity_pct > max_equity_pct:
            violations.append(
                f"portfolio equity exposure {equity_pct:.2f}% exceeds max_equity_exposure_pct {max_equity_pct:.2f}%"
            )

    min_cash_pct = _num(config.min_cash_pct, 0)
    if min_cash_pct > 0:
        cash_pct = post_cash_hkd / nav_hkd * 100
        if cash_pct < min_cash_pct:
            violations.append(f"portfolio cash after BUY {cash_pct:.2f}% is below min_cash_pct {min_cash_pct:.2f}%")

    return violations


class RiskEngine:
    def __init__(self, config: RiskConfig):
        self.config = config

    def validate(self, intent: OrderIntent) -> RiskDecision:
        violations = intent.basic_errors()
        try:
            market = intent.market
        except ValueError as exc:
            violations.append(str(exc))
            return RiskDecision(approved=False, violations=violations)

        if market not in self.config.allowed_markets:
            violations.append(f"market {market} is not enabled")

        if self.config.require_whitelist and intent.code not in self.config.allowed_codes:
            violations.append(f"code {intent.code} is not in FUTU_ALLOWED_CODES")

        if intent.side == "SELL" and not self.config.allow_sell:
            violations.append("SELL orders are disabled by FUTU_ALLOW_SELL=false")

        if intent.order_type == "MARKET" and not self.config.allow_market_orders:
            violations.append("market orders are disabled by FUTU_ALLOW_MARKET_ORDERS=false")

        max_qty = self.config.max_qty.get(market)
        if max_qty is not None and max_qty > 0 and intent.qty > max_qty:
            violations.append(f"qty {intent.qty} exceeds max qty {max_qty} for {market}")

        max_notional = self.config.max_order_value.get(market)
        if intent.side == "BUY" and max_notional is not None and max_notional > 0 and intent.notional > max_notional:
            violations.append(
                f"notional {intent.notional:.4f} exceeds max order value {max_notional:.4f} for {market}"
            )

        return RiskDecision(approved=not violations, violations=violations)

    def validate_portfolio(
        self,
        intent: OrderIntent,
        portfolio: dict[str, Any],
        *,
        positions: list[dict[str, Any]] | None = None,
        fx_to_hkd: dict[str, Any] | None = None,
    ) -> RiskDecision:
        base = self.validate(intent)
        violations = list(base.violations)
        violations.extend(
            portfolio_risk_violations(
                intent,
                portfolio,
                self.config,
                positions=positions,
                fx_to_hkd=fx_to_hkd,
            )
        )
        return RiskDecision(approved=not violations, violations=violations)
