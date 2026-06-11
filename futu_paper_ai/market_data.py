from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


EXTENDED_SESSION_SPECS = {
    "pre": {
        "price": "pre_price",
        "high": "pre_high_price",
        "low": "pre_low_price",
        "volume": "pre_volume",
        "turnover": "pre_turnover",
        "change_val": "pre_change_val",
        "change_rate": "pre_change_rate",
        "amplitude": "pre_amplitude",
    },
    "after": {
        "price": "after_price",
        "high": "after_high_price",
        "low": "after_low_price",
        "volume": "after_volume",
        "turnover": "after_turnover",
        "change_val": "after_change_val",
        "change_rate": "after_change_rate",
        "amplitude": "after_amplitude",
    },
    "overnight": {
        "price": "overnight_price",
        "high": "overnight_high_price",
        "low": "overnight_low_price",
        "volume": "overnight_volume",
        "turnover": "overnight_turnover",
        "change_val": "overnight_change_val",
        "change_rate": "overnight_change_rate",
        "amplitude": "overnight_amplitude",
    },
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except (TypeError, ValueError):
        return default


def current_us_market_phase(now: datetime | None = None) -> str:
    current = now.astimezone(ZoneInfo("America/New_York")) if now else datetime.now(ZoneInfo("America/New_York"))
    current_time = current.time()
    if time(4, 0) <= current_time < time(9, 30):
        return "pre"
    if time(9, 30) <= current_time < time(16, 0):
        return "regular"
    if time(16, 0) <= current_time < time(20, 0):
        return "after"
    return "overnight"


def _market_from_quote(quote: dict[str, Any], market: str = "") -> str:
    if market:
        return str(market).upper()
    code = str(quote.get("code") or "").upper()
    if "." in code:
        return code.split(".", 1)[0]
    return ""


def _session_payload(quote: dict[str, Any], session: str) -> dict[str, Any]:
    spec = EXTENDED_SESSION_SPECS[session]
    row = {field: _num(quote.get(source), 0) for field, source in spec.items()}
    if not any(value != 0 for value in row.values()):
        return {}
    row["session"] = session
    return row


def extended_session_from_quote(
    quote: dict[str, Any],
    market: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    if _market_from_quote(quote, market) != "US":
        return {}

    sessions = {
        session: payload
        for session in EXTENDED_SESSION_SPECS
        if (payload := _session_payload(quote, session))
    }
    if not sessions:
        return {}

    current_phase = current_us_market_phase(now)
    signal_order = {
        "pre": ("pre", "overnight", "after"),
        "regular": ("pre", "overnight", "after"),
        "after": ("after", "pre", "overnight"),
        "overnight": ("overnight", "after", "pre"),
    }.get(current_phase, ("overnight", "after", "pre"))
    signal_session = next((session for session in signal_order if session in sessions), next(iter(sessions)))
    signal = dict(sessions[signal_session])

    return {
        "available": True,
        "source": "Futu OpenD snapshot",
        "current_phase": current_phase,
        "signal_session": signal_session,
        "price": signal.get("price"),
        "change_rate": signal.get("change_rate"),
        "change_val": signal.get("change_val"),
        "volume": signal.get("volume"),
        "turnover": signal.get("turnover"),
        "sessions": sessions,
    }
