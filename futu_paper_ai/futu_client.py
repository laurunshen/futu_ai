from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

from .config import AppConfig, PROJECT_ROOT
from .market_data import extended_session_from_quote
from .models import OrderIntent
from .portfolios import DEFAULT_FX_TO_HKD, LOCAL_DEFAULT_FX_SOURCE, THIRD_PARTY_FX_SOURCE
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


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except (TypeError, ValueError):
        return default


def _quote_price(row: dict[str, Any]) -> float:
    bid = _float(row.get("bid_price"), 0)
    ask = _float(row.get("ask_price"), 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    for key in ("last_price", "nominal_price", "open_price", "prev_close_price"):
        price = _float(row.get(key), 0)
        if price > 0:
            return price
    return 0.0


def _http_json(url: str, timeout: float = 4.0) -> dict[str, Any] | list[Any]:
    request = Request(url, headers={"User-Agent": "futu-paper-ai/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _third_party_payload_from_usd_rates(
    *,
    provider: str,
    updated_at: str,
    rates_from_usd: dict[str, float],
    raw: Any,
) -> dict[str, Any]:
    usd_hkd = _float(rates_from_usd.get("HKD"), 0)
    usd_cny = _float(rates_from_usd.get("CNY"), 0)
    usd_cnh = _float(rates_from_usd.get("CNH"), 0)
    if usd_hkd <= 0:
        raise ValueError(f"{provider} returned no USD/HKD rate")

    rates = dict(DEFAULT_FX_TO_HKD)
    rates["USD"] = usd_hkd
    if usd_cny > 0:
        rates["CNY"] = usd_hkd / usd_cny
    if usd_cnh > 0:
        rates["CNH"] = usd_hkd / usd_cnh
    elif usd_cny > 0:
        rates["CNH"] = usd_hkd / usd_cny

    return {
        "ok": True,
        "source": THIRD_PARTY_FX_SOURCE,
        "provider": provider,
        "fx_to_hkd": {currency: round(value, 6) for currency, value in sorted(rates.items())},
        "quotes": [
            {"code": "USDHKD", "price": round(usd_hkd, 8), "provider": provider},
            {"code": "USDCNY", "price": round(usd_cny, 8) if usd_cny > 0 else None, "provider": provider},
            {"code": "USDCNH", "price": round(usd_cnh, 8) if usd_cnh > 0 else None, "provider": provider},
        ],
        "error": "",
        "updated_at": updated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "raw": raw,
    }


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
        rows = _records(data)
        if ret == futu.RET_OK and isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    row["extended_session"] = extended_session_from_quote(row)
        return {"ok": ret == futu.RET_OK, "data": rows}

    def fx_rates_to_hkd(self) -> dict[str, Any]:
        """Best-effort broker FX feed.

        Futu's SDK exposes an FX quote market, but some OpenD/account setups return
        "unsupported quote market". Keep the call path in place so a future OpenD
        or permission change starts using live FX without another app change.
        """
        futu = _load_futu(self.config.use_system_home)
        defaults = dict(DEFAULT_FX_TO_HKD)
        codes = [
            "FX.USDHKD",
            "FX.HKDUSD",
            "FX.CNHHKD",
            "FX.HKDCNH",
            "FX.CNYHKD",
            "FX.HKDCNY",
            "FX.USDCNH",
            "FX.CNHUSD",
            "FX.USDCNY",
            "FX.CNYUSD",
        ]
        payload: dict[str, Any] = {
            "ok": False,
            "source": LOCAL_DEFAULT_FX_SOURCE,
            "fx_to_hkd": defaults,
            "attempted_codes": codes,
            "quotes": [],
            "error": "",
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        try:
            with socket.create_connection((self.config.opend_host, self.config.opend_port), timeout=1.2):
                pass
        except OSError as exc:
            payload["error"] = f"OpenD unavailable: {exc}"
            return self._third_party_fx_rates_to_hkd(payload)

        try:
            with self.quote_context() as ctx:
                ret, data = ctx.get_market_snapshot(codes)
        except Exception as exc:
            payload["error"] = str(exc)
            return self._third_party_fx_rates_to_hkd(payload)

        if ret != futu.RET_OK:
            payload["error"] = str(data)
            return self._third_party_fx_rates_to_hkd(payload)

        rows = _records(data)
        if not isinstance(rows, list):
            payload["error"] = "Futu FX snapshot returned an unexpected payload."
            return self._third_party_fx_rates_to_hkd(payload)

        rates = dict(defaults)
        live_pairs: dict[str, float] = {}
        quote_rows: list[dict[str, Any]] = []
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                continue
            code = str(raw_row.get("code") or "").upper()
            price = _quote_price(raw_row)
            quote_rows.append(
                {
                    "code": code,
                    "name": raw_row.get("name"),
                    "update_time": raw_row.get("update_time"),
                    "price": round(price, 8) if price > 0 else None,
                }
            )
            if "." not in code or price <= 0:
                continue
            pair = code.split(".", 1)[1].replace("/", "").upper()
            if len(pair) != 6:
                continue
            base, quote = pair[:3], pair[3:]
            if base == "USD" and quote == "HKD":
                rates["USD"] = price
                live_pairs["USDHKD"] = price
            elif base == "HKD" and quote == "USD":
                rates["USD"] = 1 / price
                live_pairs["HKDUSD"] = price
            elif base in {"CNH", "CNY"} and quote == "HKD":
                rates["CNH"] = price
                rates["CNY"] = price
                live_pairs[f"{base}HKD"] = price
            elif base == "HKD" and quote in {"CNH", "CNY"}:
                rates["CNH"] = 1 / price
                rates["CNY"] = 1 / price
                live_pairs[f"HKD{quote}"] = price

        if not live_pairs:
            payload["quotes"] = quote_rows
            payload["error"] = "Futu FX snapshot returned no usable HKD cross rates."
            return self._third_party_fx_rates_to_hkd(payload)

        payload.update(
            {
                "ok": True,
                "source": "futu_opend_fx_snapshot",
                "fx_to_hkd": {currency: round(value, 6) for currency, value in sorted(rates.items())},
                "quotes": quote_rows,
                "live_pairs": live_pairs,
                "error": "",
            }
        )
        return payload

    def _third_party_fx_rates_to_hkd(self, base_payload: dict[str, Any]) -> dict[str, Any]:
        errors = [str(base_payload.get("error") or "").strip()]
        try:
            data = _http_json("https://api.frankfurter.dev/v2/rates?base=USD&quotes=HKD,CNY,CNH")
            rates: dict[str, float] = {}
            if isinstance(data, dict):
                raw_rates = data.get("rates")
                if isinstance(raw_rates, dict):
                    rates = {str(currency).upper(): _float(value, 0) for currency, value in raw_rates.items()}
                updated_at = str(data.get("date") or "")
            elif isinstance(data, list):
                updated_at = ""
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    quote = str(row.get("quote") or "").upper()
                    rate = _float(row.get("rate"), 0)
                    if quote and rate > 0:
                        rates[quote] = rate
                    updated_at = str(row.get("date") or updated_at)
            else:
                raise ValueError("unexpected Frankfurter payload")
            result = _third_party_payload_from_usd_rates(
                provider="frankfurter.dev",
                updated_at=updated_at,
                rates_from_usd=rates,
                raw=data,
            )
            result["upstream_error"] = "; ".join(error for error in errors if error)
            return result
        except Exception as exc:
            errors.append(f"frankfurter.dev: {exc}")

        try:
            data = _http_json("https://open.er-api.com/v6/latest/USD")
            if not isinstance(data, dict) or data.get("result") != "success":
                raise ValueError("unexpected open.er-api payload")
            raw_rates = data.get("rates")
            if not isinstance(raw_rates, dict):
                raise ValueError("open.er-api returned no rates")
            result = _third_party_payload_from_usd_rates(
                provider="open.er-api.com",
                updated_at=str(data.get("time_last_update_utc") or ""),
                rates_from_usd={str(currency).upper(): _float(value, 0) for currency, value in raw_rates.items()},
                raw={
                    "provider": data.get("provider"),
                    "time_last_update_utc": data.get("time_last_update_utc"),
                    "time_next_update_utc": data.get("time_next_update_utc"),
                },
            )
            result["upstream_error"] = "; ".join(error for error in errors if error)
            return result
        except Exception as exc:
            errors.append(f"open.er-api.com: {exc}")

        return {
            **base_payload,
            "error": "; ".join(error for error in errors if error),
        }

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

    def order_status(self, market: str, order_id: str, code: str = "") -> dict[str, Any]:
        futu = _load_futu(self.config.use_system_home)
        market = market.upper()
        with self.trade_context(market) as ctx:
            ret, data = ctx.order_list_query(
                order_id=str(order_id or ""),
                code=str(code or ""),
                trd_env=futu.TrdEnv.SIMULATE,
                acc_id=self.config.account_id,
                acc_index=self.config.account_index,
                refresh_cache=True,
                order_market=getattr(futu.TrdMarket, market),
            )
        return {"ok": ret == futu.RET_OK, "data": _records(data)}

    def deals(self, market: str, code: str = "", order_id: str = "") -> dict[str, Any]:
        futu = _load_futu(self.config.use_system_home)
        market = market.upper()
        with self.trade_context(market) as ctx:
            ret, data = ctx.deal_list_query(
                code=str(code or ""),
                trd_env=futu.TrdEnv.SIMULATE,
                acc_id=self.config.account_id,
                acc_index=self.config.account_index,
                refresh_cache=True,
                deal_market=getattr(futu.TrdMarket, market),
            )
        rows = _records(data)
        if ret == futu.RET_OK and order_id and isinstance(rows, list):
            rows = [row for row in rows if str(row.get("order_id") or "") == str(order_id)]
        return {"ok": ret == futu.RET_OK, "data": rows}

    def place_order(self, intent: OrderIntent, execute: bool, *, remark: str = "AI_PAPER") -> dict[str, Any]:
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
                remark=str(remark or "AI_PAPER")[:64],
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

    def place_paper_order_with_status(self, intent: OrderIntent, *, remark: str = "AI_SYNC") -> dict[str, Any]:
        placed = self.place_order(intent, execute=True, remark=remark)
        if not placed.get("ok"):
            return {**placed, "mode": "futu_sync"}

        rows = placed.get("data") or []
        first = rows[0] if isinstance(rows, list) and rows else {}
        order_id = str(first.get("order_id") or "")
        time.sleep(0.6)

        order_payload = self.order_status(intent.market, order_id, intent.code) if order_id else {"ok": False, "data": []}
        order_rows = order_payload.get("data") if order_payload.get("ok") else []
        order = order_rows[0] if isinstance(order_rows, list) and order_rows else first
        deals_payload = self.deals(intent.market, intent.code, order_id) if order_id else {"ok": False, "data": []}

        return {
            "ok": True,
            "mode": "futu_sync",
            "intent": intent.to_dict(),
            "place": placed,
            "order_id": order_id,
            "order": order,
            "deals": deals_payload.get("data") or [],
            "order_query": order_payload,
        }
