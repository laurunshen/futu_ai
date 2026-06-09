from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CODE_PATTERN = re.compile(r"^(US\.[A-Z0-9.]+|HK\.[A-Z0-9]+|SH\.[0-9]{6}|SZ\.[0-9]{6})$")


def infer_market(code: str) -> str:
    code = code.upper()
    if code.startswith("US."):
        return "US"
    if code.startswith("HK."):
        return "HK"
    if code.startswith(("SH.", "SZ.")):
        return "CN"
    raise ValueError(f"Unsupported Futu code prefix: {code}")


@dataclass(frozen=True)
class OrderIntent:
    code: str
    side: str
    qty: float
    price: float
    order_type: str = "NORMAL"
    reason: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OrderIntent":
        try:
            code = str(payload["code"]).strip().upper()
            side = str(payload["side"]).strip().upper()
            qty = float(payload["qty"])
            price = float(payload["price"])
        except KeyError as exc:
            raise ValueError(f"Missing required order intent field: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric field in order intent: {exc}") from exc

        order_type = str(payload.get("order_type", "NORMAL")).strip().upper()
        reason = str(payload.get("reason", "")).strip()
        return cls(code=code, side=side, qty=qty, price=price, order_type=order_type, reason=reason)

    @classmethod
    def from_file(cls, path: str | Path) -> "OrderIntent":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Order intent file must contain a JSON object")
        return cls.from_dict(payload)

    @property
    def market(self) -> str:
        return infer_market(self.code)

    @property
    def notional(self) -> float:
        return self.qty * self.price

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        try:
            data["market"] = self.market
        except ValueError:
            data["market"] = "UNKNOWN"
        data["notional"] = self.notional
        return data

    def basic_errors(self) -> list[str]:
        errors: list[str] = []
        if not CODE_PATTERN.match(self.code):
            errors.append("code must look like US.AAPL, HK.00700, SH.600519, or SZ.000001")
        if self.side not in {"BUY", "SELL"}:
            errors.append("side must be BUY or SELL")
        if self.qty <= 0:
            errors.append("qty must be greater than 0")
        if self.price <= 0:
            errors.append("price must be greater than 0")
        if self.order_type not in {"NORMAL", "LIMIT", "MARKET"}:
            errors.append("order_type must be NORMAL, LIMIT, or MARKET")
        return errors
