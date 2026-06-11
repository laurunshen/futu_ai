from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class MarketSession:
    market: str
    status: str
    timezone: str
    local_time: str
    can_trade: bool
    should_scan: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "status": self.status,
            "timezone": self.timezone,
            "local_time": self.local_time,
            "can_trade": self.can_trade,
            "should_scan": self.should_scan,
            "reason": self.reason,
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


def _in_windows(current_time: time, windows: tuple[tuple[time, time], ...]) -> bool:
    return any(start <= current_time < end for start, end in windows)


def market_session(market: str, now: datetime | None = None) -> MarketSession:
    market_key = str(market or "").upper()
    if market_key == "US":
        timezone_name = "America/New_York"
        regular_windows = ((time(9, 30), time(16, 0)),)
        prep_windows = ((time(9, 0), time(9, 30)),)
    elif market_key == "HK":
        timezone_name = "Asia/Hong_Kong"
        regular_windows = ((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0)))
        prep_windows = ((time(9, 15), time(9, 30)), (time(12, 45), time(13, 0)))
    elif market_key == "CN":
        timezone_name = "Asia/Shanghai"
        regular_windows = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
        prep_windows = ((time(9, 15), time(9, 30)), (time(12, 45), time(13, 0)))
    else:
        timezone_name = "UTC"
        regular_windows = ()
        prep_windows = ()

    zone = ZoneInfo(timezone_name)
    current = now.astimezone(zone) if now else datetime.now(zone)
    current_time = current.time()
    local_time = current.isoformat(timespec="seconds")
    if current.weekday() >= 5:
        return MarketSession(market_key, "closed", timezone_name, local_time, False, False, "weekend")
    if _in_windows(current_time, regular_windows):
        return MarketSession(market_key, "regular", timezone_name, local_time, True, True, "regular trading session")
    if _in_windows(current_time, prep_windows):
        return MarketSession(market_key, "preopen", timezone_name, local_time, False, True, "pre-open decision preparation")
    return MarketSession(market_key, "closed", timezone_name, local_time, False, False, "outside configured trading windows")


def market_session_payload(market: str, now: datetime | None = None) -> dict[str, Any]:
    return market_session(market, now).to_dict()


def auto_loop_session(markets: set[str] | list[str] | tuple[str, ...], now: datetime | None = None) -> dict[str, Any]:
    sessions = [market_session(str(market), now).to_dict() for market in sorted({str(item).upper() for item in markets})]
    scan_markets = [session["market"] for session in sessions if session["should_scan"]]
    trade_markets = [session["market"] for session in sessions if session["can_trade"]]
    prep_markets = [session["market"] for session in sessions if session["status"] == "preopen"]
    return {
        "should_scan": bool(scan_markets),
        "can_apply_orders": bool(trade_markets),
        "scan_markets": scan_markets,
        "trade_markets": trade_markets,
        "prep_markets": prep_markets,
        "sessions": sessions,
        "note": (
            "regular sessions can scan and apply orders; preopen sessions scan only; closed sessions are skipped"
        ),
    }


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
