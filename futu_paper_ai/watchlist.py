from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .models import infer_market


DEFAULT_WATCHLIST_PATH = PROJECT_ROOT / "data" / "watchlist.default.json"
USER_WATCHLIST_PATH = PROJECT_ROOT / "data" / "state" / "watchlist.user.json"
DEFAULT_USER_WATCHLIST = [
    {"code": "US.NVDA", "name": "NVIDIA", "sector": "AI Chips"},
    {"code": "US.AAPL", "name": "Apple", "sector": "Consumer Electronics"},
    {"code": "US.TSLA", "name": "Tesla", "sector": "EV"},
    {"code": "HK.00700", "name": "Tencent", "sector": "Internet"},
    {"code": "HK.09988", "name": "Alibaba", "sector": "E-commerce"},
]


@dataclass(frozen=True)
class WatchItem:
    code: str
    name: str
    sector: str
    market: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WatchItem":
        code = str(payload["code"]).strip().upper()
        return cls(
            code=code,
            name=str(payload.get("name", "")).strip(),
            sector=str(payload.get("sector", "Other")).strip(),
            market=str(payload.get("market", infer_market(code))).strip().upper(),
        )


def load_watchlist(path: Path | None = None, markets: set[str] | None = None) -> list[WatchItem]:
    watchlist_path = path or DEFAULT_WATCHLIST_PATH
    payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("watchlist must be a JSON array")
    items = [WatchItem.from_dict(item) for item in payload]
    if markets:
        items = [item for item in items if item.market in markets]
    return items


def codes_for_markets(markets: set[str] | None = None) -> list[str]:
    return [item.code for item in load_watchlist(markets=markets)]


def load_user_watchlist(path: Path | None = None) -> list[WatchItem]:
    watchlist_path = path or USER_WATCHLIST_PATH
    if not watchlist_path.exists():
        save_user_watchlist([WatchItem.from_dict(item) for item in DEFAULT_USER_WATCHLIST], watchlist_path)
    return load_watchlist(watchlist_path)


def save_user_watchlist(items: list[WatchItem], path: Path | None = None) -> None:
    watchlist_path = path or USER_WATCHLIST_PATH
    watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    deduped: dict[str, WatchItem] = {}
    for item in items:
        deduped[item.code] = item
    payload = [item.__dict__ for item in sorted(deduped.values(), key=lambda row: row.code)]
    watchlist_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add_user_watch(code: str, name: str = "", sector: str = "Other") -> list[WatchItem]:
    item = WatchItem.from_dict({"code": code, "name": name, "sector": sector})
    items = [existing for existing in load_user_watchlist() if existing.code != item.code]
    items.append(item)
    save_user_watchlist(items)
    return load_user_watchlist()


def remove_user_watch(code: str) -> list[WatchItem]:
    normalized = str(code).strip().upper()
    items = [item for item in load_user_watchlist() if item.code != normalized]
    save_user_watchlist(items)
    return load_user_watchlist()
