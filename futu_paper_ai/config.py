from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _split_csv(value: str) -> set[str]:
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class RiskConfig:
    allowed_markets: set[str]
    allowed_codes: set[str]
    require_whitelist: bool
    allow_sell: bool
    allow_market_orders: bool
    max_order_value: dict[str, float]
    max_qty: dict[str, float]


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str
    auto_enabled: bool
    auto_execute: bool
    observe_markets: set[str]
    execute_markets: set[str]
    confidence_threshold: int
    max_notional: dict[str, float]
    max_trades_per_day: int
    cooldown_minutes: int
    loop_interval_seconds: int
    candidate_count: int


@dataclass(frozen=True)
class AppConfig:
    opend_host: str
    opend_port: int
    use_system_home: bool
    security_firm: str
    account_id: int
    account_index: int
    risk: RiskConfig
    gemini: GeminiConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()
        return cls(
            opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
            opend_port=_int_env("FUTU_OPEND_PORT", 11111),
            use_system_home=_bool_env("FUTU_USE_SYSTEM_HOME", False),
            security_firm=os.environ.get("FUTU_SECURITY_FIRM", "").strip().upper(),
            account_id=_int_env("FUTU_ACCOUNT_ID", 0),
            account_index=_int_env("FUTU_ACCOUNT_INDEX", 0),
            risk=RiskConfig(
                allowed_markets=_split_csv(os.environ.get("FUTU_ALLOWED_MARKETS", "US,HK,CN")),
                allowed_codes=_split_csv(
                    os.environ.get("FUTU_ALLOWED_CODES", "US.AAPL,HK.00700,SH.600519,SZ.000001")
                ),
                require_whitelist=_bool_env("FUTU_REQUIRE_WHITELIST", True),
                allow_sell=_bool_env("FUTU_ALLOW_SELL", True),
                allow_market_orders=_bool_env("FUTU_ALLOW_MARKET_ORDERS", False),
                max_order_value={
                    "US": _float_env("FUTU_MAX_ORDER_VALUE_US", 1000.0),
                    "HK": _float_env("FUTU_MAX_ORDER_VALUE_HK", 10000.0),
                    "CN": _float_env("FUTU_MAX_ORDER_VALUE_CN", 10000.0),
                },
                max_qty={
                    "US": _float_env("FUTU_MAX_QTY_US", 10.0),
                    "HK": _float_env("FUTU_MAX_QTY_HK", 1000.0),
                    "CN": _float_env("FUTU_MAX_QTY_CN", 1000.0),
                },
            ),
            gemini=GeminiConfig(
                api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
                model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip(),
                auto_enabled=_bool_env("GEMINI_AUTO_ENABLED", False),
                auto_execute=_bool_env("GEMINI_AUTO_EXECUTE", False),
                observe_markets=_split_csv(os.environ.get("GEMINI_OBSERVE_MARKETS", "US,HK")),
                execute_markets=_split_csv(os.environ.get("GEMINI_EXECUTE_MARKETS", "US")),
                confidence_threshold=_int_env("GEMINI_CONFIDENCE_THRESHOLD", 70),
                max_notional={
                    "US": _float_env("GEMINI_MAX_NOTIONAL_US", 300.0),
                    "HK": _float_env("GEMINI_MAX_NOTIONAL_HK", 1000.0),
                    "CN": _float_env("GEMINI_MAX_NOTIONAL_CN", 1000.0),
                },
                max_trades_per_day=_int_env("GEMINI_MAX_TRADES_PER_DAY", 3),
                cooldown_minutes=_int_env("GEMINI_COOLDOWN_MINUTES", 60),
                loop_interval_seconds=_int_env("GEMINI_LOOP_INTERVAL_SECONDS", 300),
                candidate_count=_int_env("GEMINI_CANDIDATE_COUNT", 8),
            ),
        )


def public_config(config: AppConfig) -> dict:
    payload = asdict(config)
    gemini = payload.get("gemini", {})
    if gemini.get("api_key"):
        gemini["api_key"] = "***set***"
    else:
        gemini["api_key"] = ""
    return payload
