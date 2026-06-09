from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .models import infer_market


DEFAULT_WATCHLIST_PATH = PROJECT_ROOT / "data" / "watchlist.default.json"


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
