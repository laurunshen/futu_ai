from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .storage import atomic_write_text, file_lock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = PROJECT_ROOT / "data" / "state"
RISK_CONFIG_PATH = STATE_ROOT / "risk_config.json"
PORTFOLIOS_PATH = STATE_ROOT / "portfolios.json"
PORTFOLIOS_DB_PATH = STATE_ROOT / "portfolios.db"
PORTFOLIOS_DB_KEY = "portfolios"
WATCHLIST_DEFAULT_PATH = PROJECT_ROOT / "data" / "watchlist.default.json"
WATCHLIST_USER_PATH = STATE_ROOT / "watchlist.user.json"


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


def _read_json(path: Path, *, locked: bool = False) -> object:
    if not path.exists():
        return None
    try:
        if locked:
            with file_lock(path, exclusive=False):
                return json.loads(path.read_text(encoding="utf-8"))
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _codes_from_watchlist(path: Path) -> set[str]:
    payload = _read_json(path)
    if not isinstance(payload, list):
        return set()
    codes: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "trade").strip().lower()
        if role == "context":
            continue
        code = str(item.get("code") or "").strip().upper()
        if code:
            codes.add(code)
    return codes


def _codes_from_portfolios(path: Path = PORTFOLIOS_PATH) -> set[str]:
    payload = _read_portfolios_payload(path)
    if not isinstance(payload, dict):
        return set()
    codes: set[str] = set()
    for portfolio in payload.get("portfolios") or []:
        if not isinstance(portfolio, dict):
            continue
        for position in portfolio.get("positions") or []:
            if not isinstance(position, dict):
                continue
            code = str(position.get("code") or "").strip().upper()
            if code:
                codes.add(code)
    return codes


def _read_portfolios_payload(path: Path = PORTFOLIOS_PATH) -> object:
    if PORTFOLIOS_DB_PATH.exists():
        conn = None
        try:
            conn = sqlite3.connect(f"file:{PORTFOLIOS_DB_PATH}?mode=ro", uri=True, timeout=2)
            conn.execute("PRAGMA busy_timeout=2000")
            row = conn.execute(
                "SELECT value FROM portfolio_state WHERE key = ?",
                (PORTFOLIOS_DB_KEY,),
            ).fetchone()
            if row is not None:
                return json.loads(str(row[0]))
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            pass
        finally:
            if conn is not None:
                conn.close()
    return _read_json(path)


def _auto_allowed_codes() -> set[str]:
    """Keep the safety whitelist aligned with the configured trading universe and held positions."""
    return (
        _codes_from_watchlist(WATCHLIST_DEFAULT_PATH)
        | _codes_from_watchlist(WATCHLIST_USER_PATH)
        | _codes_from_portfolios()
    )


def save_runtime_risk_config(payload: dict) -> RiskConfig:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    base = AppConfig.from_env().risk
    normalized = _risk_config_from_payload(payload, base)
    with file_lock(RISK_CONFIG_PATH):
        atomic_write_text(
            RISK_CONFIG_PATH,
            json.dumps(_risk_config_to_payload(normalized), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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
    max_position_pct: float
    max_equity_exposure_pct: float
    min_cash_pct: float


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str
    agent_mode: str
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
    chat_max_output_tokens: int


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
                "US": _float_env("FUTU_MAX_ORDER_VALUE_US", 0.0),
                "HK": _float_env("FUTU_MAX_ORDER_VALUE_HK", 0.0),
                "CN": _float_env("FUTU_MAX_ORDER_VALUE_CN", 0.0),
            },
            max_qty={
                "US": _float_env("FUTU_MAX_QTY_US", 0.0),
                "HK": _float_env("FUTU_MAX_QTY_HK", 0.0),
                "CN": _float_env("FUTU_MAX_QTY_CN", 0.0),
            },
            max_position_pct=_float_env("FUTU_MAX_POSITION_PCT", 0.0),
            max_equity_exposure_pct=_float_env("FUTU_MAX_EQUITY_EXPOSURE_PCT", 0.0),
            min_cash_pct=_float_env("FUTU_MIN_CASH_PCT", 0.0),
        )
        risk = _risk_config_from_payload(_read_runtime_risk_config(), base_risk)
        extra_allowed_codes = _auto_allowed_codes()
        if extra_allowed_codes:
            risk = replace(risk, allowed_codes=set(risk.allowed_codes) | extra_allowed_codes)
        return cls(
            opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
            opend_port=_int_env("FUTU_OPEND_PORT", 11111),
            use_system_home=_bool_env("FUTU_USE_SYSTEM_HOME", False),
            security_firm=os.environ.get("FUTU_SECURITY_FIRM", "").strip().upper(),
            account_id=_int_env("FUTU_ACCOUNT_ID", 0),
            account_index=_int_env("FUTU_ACCOUNT_INDEX", 0),
            risk=risk,
            gemini=GeminiConfig(
                api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
                model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip(),
                agent_mode=os.environ.get("GEMINI_AGENT_MODE", "multi_lite").strip().lower() or "multi_lite",
                auto_enabled=_bool_env("GEMINI_AUTO_ENABLED", False),
                auto_execute=_bool_env("GEMINI_AUTO_EXECUTE", False),
                observe_markets=_split_csv(os.environ.get("GEMINI_OBSERVE_MARKETS", "US,HK")),
                execute_markets=_split_csv(os.environ.get("GEMINI_EXECUTE_MARKETS", "US,HK")),
                confidence_threshold=_int_env("GEMINI_CONFIDENCE_THRESHOLD", 70),
                max_notional={
                    "US": _float_env("GEMINI_MAX_NOTIONAL_US", 0.0),
                    "HK": _float_env("GEMINI_MAX_NOTIONAL_HK", 0.0),
                    "CN": _float_env("GEMINI_MAX_NOTIONAL_CN", 0.0),
                },
                max_trades_per_day=_int_env("GEMINI_MAX_TRADES_PER_DAY", 0),
                cooldown_minutes=_int_env("GEMINI_COOLDOWN_MINUTES", 0),
                loop_interval_seconds=_int_env("GEMINI_LOOP_INTERVAL_SECONDS", 300),
                candidate_count=_int_env("GEMINI_CANDIDATE_COUNT", 8),
                chat_max_output_tokens=_int_env("GEMINI_CHAT_MAX_OUTPUT_TOKENS", 8000),
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

    def float_value(key: str, default: float) -> float:
        try:
            return max(0.0, float(payload.get(key, default)))
        except (TypeError, ValueError):
            return default

    return RiskConfig(
        allowed_markets=set_value("allowed_markets", base.allowed_markets),
        allowed_codes=set_value("allowed_codes", base.allowed_codes),
        require_whitelist=bool_value("require_whitelist", base.require_whitelist),
        allow_sell=bool_value("allow_sell", base.allow_sell),
        allow_market_orders=bool_value("allow_market_orders", base.allow_market_orders),
        max_order_value=float_map("max_order_value", base.max_order_value),
        max_qty=float_map("max_qty", base.max_qty),
        max_position_pct=float_value("max_position_pct", base.max_position_pct),
        max_equity_exposure_pct=float_value("max_equity_exposure_pct", base.max_equity_exposure_pct),
        min_cash_pct=float_value("min_cash_pct", base.min_cash_pct),
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
        "max_position_pct": risk.max_position_pct,
        "max_equity_exposure_pct": risk.max_equity_exposure_pct,
        "min_cash_pct": risk.min_cash_pct,
    }


def public_config(config: AppConfig) -> dict:
    payload = asdict(config)
    gemini = payload.get("gemini", {})
    if gemini.get("api_key"):
        gemini["api_key"] = "***set***"
    else:
        gemini["api_key"] = ""
    return payload
