from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = PROJECT_ROOT / "data" / "state"
RISK_CONFIG_PATH = STATE_ROOT / "risk_config.json"


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


def _read_runtime_risk_config() -> dict:
    if not RISK_CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(RISK_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_runtime_risk_config(payload: dict) -> RiskConfig:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    base = AppConfig.from_env().risk
    normalized = _risk_config_from_payload(payload, base)
    RISK_CONFIG_PATH.write_text(json.dumps(_risk_config_to_payload(normalized), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


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
class NewsConfig:
    autonews_db_path: str
    lookback_hours: int
    min_impact: int
    max_signals: int


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
    news: NewsConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()
        base_risk = RiskConfig(
            allowed_markets=_split_csv(os.environ.get("FUTU_ALLOWED_MARKETS", "US,HK,CN")),
            allowed_codes=_split_csv(os.environ.get("FUTU_ALLOWED_CODES", "US.AAPL,HK.00700,SH.600519,SZ.000001")),
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
        )
        return cls(
            opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
            opend_port=_int_env("FUTU_OPEND_PORT", 11111),
            use_system_home=_bool_env("FUTU_USE_SYSTEM_HOME", False),
            security_firm=os.environ.get("FUTU_SECURITY_FIRM", "").strip().upper(),
            account_id=_int_env("FUTU_ACCOUNT_ID", 0),
            account_index=_int_env("FUTU_ACCOUNT_INDEX", 0),
            risk=_risk_config_from_payload(_read_runtime_risk_config(), base_risk),
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
            news=NewsConfig(
                autonews_db_path=os.environ.get("AUTONEWS_DB_PATH", "").strip(),
                lookback_hours=_int_env("AUTONEWS_LOOKBACK_HOURS", 24),
                min_impact=_int_env("AUTONEWS_MIN_IMPACT", 60),
                max_signals=_int_env("AUTONEWS_MAX_SIGNALS", 8),
            ),
        )


def _risk_config_from_payload(payload: dict, base: RiskConfig) -> RiskConfig:
    def bool_value(key: str, default: bool) -> bool:
        value = payload.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    def set_value(key: str, default: set[str]) -> set[str]:
        value = payload.get(key, default)
        if isinstance(value, str):
            return _split_csv(value)
        if isinstance(value, (list, tuple, set)):
            return {str(item).strip().upper() for item in value if str(item).strip()}
        return set(default)

    def float_map(key: str, default: dict[str, float]) -> dict[str, float]:
        value = payload.get(key, default)
        if not isinstance(value, dict):
            return dict(default)
        result = dict(default)
        for market in ("US", "HK", "CN"):
            if market not in value:
                continue
            try:
                result[market] = max(0.0, float(value[market]))
            except (TypeError, ValueError):
                pass
        return result

    return RiskConfig(
        allowed_markets=set_value("allowed_markets", base.allowed_markets),
        allowed_codes=set_value("allowed_codes", base.allowed_codes),
        require_whitelist=bool_value("require_whitelist", base.require_whitelist),
        allow_sell=bool_value("allow_sell", base.allow_sell),
        allow_market_orders=bool_value("allow_market_orders", base.allow_market_orders),
        max_order_value=float_map("max_order_value", base.max_order_value),
        max_qty=float_map("max_qty", base.max_qty),
    )


def _risk_config_to_payload(risk: RiskConfig) -> dict:
    return {
        "allowed_markets": sorted(risk.allowed_markets),
        "allowed_codes": sorted(risk.allowed_codes),
        "require_whitelist": risk.require_whitelist,
        "allow_sell": risk.allow_sell,
        "allow_market_orders": risk.allow_market_orders,
        "max_order_value": dict(risk.max_order_value),
        "max_qty": dict(risk.max_qty),
    }


def public_config(config: AppConfig) -> dict:
    payload = asdict(config)
    gemini = payload.get("gemini", {})
    if gemini.get("api_key"):
        gemini["api_key"] = "***set***"
    else:
        gemini["api_key"] = ""
    return payload
