from __future__ import annotations

from dataclasses import dataclass

from .config import RiskConfig
from .models import OrderIntent


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    violations: list[str]


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
        if max_qty is not None and intent.qty > max_qty:
            violations.append(f"qty {intent.qty} exceeds max qty {max_qty} for {market}")

        max_notional = self.config.max_order_value.get(market)
        if max_notional is not None and intent.notional > max_notional:
            violations.append(
                f"notional {intent.notional:.4f} exceeds max order value {max_notional:.4f} for {market}"
            )

        return RiskDecision(approved=not violations, violations=violations)
