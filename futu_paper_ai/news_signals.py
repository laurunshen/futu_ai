from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import NewsConfig


@dataclass(frozen=True)
class NewsSignal:
    id: int
    url: str
    topic_name: str
    title: str
    summary: str
    so_what: str
    impact_score: int
    tickers: list[str]
    affected_markets: list[str]
    asset_classes: list[str]
    direction: str
    horizon: str
    confidence: float
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "topic_name": self.topic_name,
            "title": self.title,
            "summary": self.summary,
            "so_what": self.so_what,
            "impact_score": self.impact_score,
            "tickers": self.tickers,
            "affected_markets": self.affected_markets,
            "asset_classes": self.asset_classes,
            "direction": self.direction,
            "horizon": self.horizon,
            "confidence": self.confidence,
            "created_at": self.created_at,
        }


def _parse_json_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        cleaned = str(value).strip().strip("[]")
        return [item.strip().strip("\"'") for item in cleaned.split(",") if item.strip().strip("\"'")]
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _parse_created_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_news_signals(config: NewsConfig) -> dict[str, Any]:
    db_path = Path(config.autonews_db_path).expanduser()
    if not str(db_path) or not config.autonews_db_path:
        return {"ok": True, "enabled": False, "signals": [], "notes": [], "message": "AUTONEWS_DB_PATH is not set."}
    if not db_path.exists():
        return {
            "ok": True,
            "enabled": True,
            "signals": [],
            "notes": [],
            "db_path": str(db_path),
            "message": "autoNews signal database does not exist yet.",
        }

    cutoff = datetime.now() - timedelta(hours=max(config.lookback_hours, 1))
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                """
                SELECT id, url, topic_name, title_cn, summary, so_what, impact_score, tickers,
                       affected_markets, asset_classes, direction, horizon, confidence, created_at
                FROM signals
                WHERE impact_score >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(config.min_impact, 0), max(config.max_signals * 4, config.max_signals)),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "enabled": True,
            "signals": [],
            "notes": [],
            "db_path": str(db_path),
            "error": str(exc),
        }

    signals: list[NewsSignal] = []
    for row in rows:
        created_at = str(row["created_at"] or "")
        parsed_created_at = _parse_created_at(created_at)
        if parsed_created_at and parsed_created_at < cutoff:
            continue
        signals.append(
            NewsSignal(
                id=int(row["id"] or 0),
                url=str(row["url"] or ""),
                topic_name=str(row["topic_name"] or ""),
                title=str(row["title_cn"] or ""),
                summary=str(row["summary"] or ""),
                so_what=str(row["so_what"] or ""),
                impact_score=int(row["impact_score"] or 0),
                tickers=_parse_json_list(row["tickers"]),
                affected_markets=_parse_json_list(row["affected_markets"]),
                asset_classes=_parse_json_list(row["asset_classes"]),
                direction=str(row["direction"] or ""),
                horizon=str(row["horizon"] or ""),
                confidence=float(row["confidence"] or 0),
                created_at=created_at,
            )
        )
        if len(signals) >= max(config.max_signals, 1):
            break

    notes = [format_signal_note(signal) for signal in signals]
    return {
        "ok": True,
        "enabled": True,
        "db_path": str(db_path),
        "lookback_hours": config.lookback_hours,
        "min_impact": config.min_impact,
        "count": len(signals),
        "signals": [signal.to_dict() for signal in signals],
        "notes": notes,
    }


def format_signal_note(signal: NewsSignal) -> str:
    tickers = ", ".join(signal.tickers) if signal.tickers else "无明确标的"
    markets = ", ".join(signal.affected_markets) if signal.affected_markets else "未知市场"
    assets = ", ".join(signal.asset_classes) if signal.asset_classes else "未知资产"
    parts = [
        f"[autoNews] impact={signal.impact_score} confidence={signal.confidence:.2f}",
        f"title={signal.title or '(无标题)'}",
        f"tickers={tickers}",
        f"markets={markets}",
        f"assets={assets}",
    ]
    if signal.direction:
        parts.append(f"direction={signal.direction}")
    if signal.horizon:
        parts.append(f"horizon={signal.horizon}")
    if signal.so_what:
        parts.append(f"so_what={signal.so_what}")
    if signal.summary:
        parts.append(f"summary={signal.summary[:500]}")
    return " | ".join(parts)
