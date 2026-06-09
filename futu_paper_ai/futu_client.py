from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import AppConfig, PROJECT_ROOT
from .models import OrderIntent
from .risk import RiskDecision, RiskEngine


def _prepare_sdk_home(use_system_home: bool) -> None:
    if use_system_home:
        return

    sdk_home = Path(os.environ.get("FUTU_PAPER_AI_HOME", PROJECT_ROOT / ".runtime" / "home"))
    sdk_home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(sdk_home)


def _load_futu(use_system_home: bool = False) -> Any:
    _prepare_sdk_home(use_system_home)
    try:
        import futu
    except ImportError as exc:
        raise RuntimeError("futu-api is not installed. Run: pip install -r requirements.txt") from exc
    return futu


def _records(data: Any) -> Any:
    if hasattr(data, "to_dict"):
        return data.to_dict(orient="records")
    return data


class FutuPaperClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.risk = RiskEngine(config.risk)

    def validate(self, intent: OrderIntent) -> RiskDecision:
        return self.risk.validate(intent)

    @contextmanager
    def quote_context(self) -> Iterator[Any]:
        futu = _load_futu(self.config.use_system_home)
        ctx = futu.OpenQuoteContext(host=self.config.opend_host, port=self.config.opend_port)
        try:
            yield ctx
        finally:
            ctx.close()

    @contextmanager
    def trade_context(self, market: str) -> Iterator[Any]:
        futu = _load_futu(self.config.use_system_home)
        kwargs: dict[str, Any] = {
            "host": self.config.opend_host,
            "port": self.config.opend_port,
            "filter_trdmarket": getattr(futu.TrdMarket, market),
        }
        if self.config.security_firm:
            kwargs["security_firm"] = getattr(futu.SecurityFirm, self.config.security_firm)

        ctx = futu.OpenSecTradeContext(**kwargs)
        try:
            yield ctx
        finally:
            ctx.close()

    def snapshot(self, codes: list[str]) -> dict[str, Any]:
        futu = _load_futu(self.config.use_system_home)
        with self.quote_context() as ctx:
            ret, data = ctx.get_market_snapshot([code.upper() for code in codes])
        return {"ok": ret == futu.RET_OK, "data": _records(data)}

    def account(self, market: str, currency: str) -> dict[str, Any]:
        futu = _load_futu(self.config.use_system_home)
        market = market.upper()
        currency = currency.upper()
        with self.trade_context(market) as ctx:
            ret, data = ctx.accinfo_query(
                trd_env=futu.TrdEnv.SIMULATE,
                acc_id=self.config.account_id,
                acc_index=self.config.account_index,
                currency=getattr(futu.Currency, currency),
            )
        return {"ok": ret == futu.RET_OK, "data": _records(data)}

    def positions(self, market: str) -> dict[str, Any]:
        futu = _load_futu(self.config.use_system_home)
        market = market.upper()
        with self.trade_context(market) as ctx:
            ret, data = ctx.position_list_query(
                trd_env=futu.TrdEnv.SIMULATE,
                acc_id=self.config.account_id,
                acc_index=self.config.account_index,
            )
        return {"ok": ret == futu.RET_OK, "data": _records(data)}

    def place_order(self, intent: OrderIntent, execute: bool) -> dict[str, Any]:
        decision = self.validate(intent)
        if not decision.approved:
            return {
                "ok": False,
                "mode": "dry_run" if not execute else "blocked",
                "intent": intent.to_dict(),
                "violations": decision.violations,
            }

        if not execute:
            return {
                "ok": True,
                "mode": "dry_run",
                "message": "Risk checks passed. Add --execute to submit to Futu paper trading.",
                "intent": intent.to_dict(),
            }

        futu = _load_futu(self.config.use_system_home)
        order_type = "NORMAL" if intent.order_type == "LIMIT" else intent.order_type
        with self.trade_context(intent.market) as ctx:
            ret, data = ctx.place_order(
                price=intent.price,
                qty=intent.qty,
                code=intent.code,
                trd_side=getattr(futu.TrdSide, intent.side),
                order_type=getattr(futu.OrderType, order_type),
                trd_env=futu.TrdEnv.SIMULATE,
                acc_id=self.config.account_id,
                acc_index=self.config.account_index,
                remark="AI_PAPER",
                time_in_force=futu.TimeInForce.DAY,
                fill_outside_rth=False,
                session=futu.Session.NONE,
            )

        return {
            "ok": ret == futu.RET_OK,
            "mode": "paper_execute",
            "intent": intent.to_dict(),
            "data": _records(data),
        }
