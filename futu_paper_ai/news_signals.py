from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import NewsConfig


MACRO_MARKETS = {"商品期货", "股指期货", "外汇", "利率债", "黄金", "原油", "加密", "全球"}
MACRO_ASSETS = {"CPI", "NFP", "非农", "美债", "美元", "人民币汇率", "原油", "黄金", "铜", "铁矿", "关税", "制裁"}


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
    normalized_tickers: list[str]
    affected_markets: list[str]
    asset_classes: list[str]
    direction: str
    horizon: str
    confidence: float
    created_at: str
    match_type: str = "general"
    matched_codes: list[str] | None = None

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
            "normalized_tickers": self.normalized_tickers,
            "affected_markets": self.affected_markets,
            "asset_classes": self.asset_classes,
            "direction": self.direction,
            "horizon": self.horizon,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "match_type": self.match_type,
            "matched_codes": self.matched_codes or [],
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


def normalize_ticker(value: str) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    text = text.strip("\"'")
    if not text:
        return ""

    if text.startswith("US."):
        return f"US.{text[3:]}"
    if text.startswith("HK."):
        raw = text[3:]
        return f"HK.{raw.zfill(5)}" if raw.isdigit() else f"HK.{raw}"
    if text.startswith(("SH.", "SZ.")):
        market, raw = text.split(".", 1)
        return f"{market}.{raw.zfill(6)}" if raw.isdigit() else f"{market}.{raw}"

    if text.endswith(".HK"):
        raw = text[:-3]
        return f"HK.{raw.zfill(5)}" if raw.isdigit() else ""
    if text.endswith(".SH"):
        raw = text[:-3]
        return f"SH.{raw.zfill(6)}" if raw.isdigit() else ""
    if text.endswith(".SZ"):
        raw = text[:-3]
        return f"SZ.{raw.zfill(6)}" if raw.isdigit() else ""

    hk_match = re.fullmatch(r"HK(\d{1,5})", text)
    if hk_match:
        return f"HK.{hk_match.group(1).zfill(5)}"

    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", text):
        return f"US.{text}"
    return ""


def _normalize_codes(values: list[str] | tuple[str, ...] | set[str] | None) -> set[str]:
    result = set()
    for value in values or []:
        code = normalize_ticker(value)
        if code:
            result.add(code)
    return result


def _is_macro_signal(signal: NewsSignal, macro_impact_floor: int) -> bool:
    if signal.impact_score < macro_impact_floor:
        return False
    markets = set(signal.affected_markets)
    assets = set(signal.asset_classes)
    if signal.normalized_tickers and not (markets & MACRO_MARKETS or assets & MACRO_ASSETS):
        return False
    return bool(markets & MACRO_MARKETS or assets & MACRO_ASSETS or not signal.normalized_tickers)


def _rank_signal(signal: NewsSignal, candidate_codes: set[str], focus_codes: set[str], min_impact: int) -> tuple[bool, NewsSignal, float]:
    matched_candidate = sorted(set(signal.normalized_tickers) & candidate_codes)
    matched_focus = sorted(set(signal.normalized_tickers) & focus_codes)
    macro_impact_floor = max(75, min_impact + 15)

    if matched_candidate:
        match_type = "candidate"
        matched_codes = matched_candidate
        base = 2000
    elif matched_focus:
        match_type = "watchlist"
        matched_codes = matched_focus
        base = 1200
    elif candidate_codes or focus_codes:
        if not _is_macro_signal(signal, macro_impact_floor):
            return False, signal, 0
        match_type = "macro"
        matched_codes = []
        base = 500
    else:
        match_type = "general"
        matched_codes = []
        base = 0

    ranked = NewsSignal(
        **{
            **signal.to_dict(),
            "matched_codes": matched_codes,
            "match_type": match_type,
        }
    )
    score = base + signal.impact_score * 10 + signal.confidence * 100 + min(signal.id, 10_000) / 1000
    return True, ranked, score


def load_news_signals(
    config: NewsConfig,
    *,
    focus_codes: list[str] | tuple[str, ...] | set[str] | None = None,
    candidate_codes: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
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
    focus_code_set = _normalize_codes(focus_codes)
    candidate_code_set = _normalize_codes(candidate_codes)
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
                (max(config.min_impact, 0), max(config.max_signals * 10, 100)),
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

    ranked_signals: list[tuple[float, NewsSignal]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for row in rows:
        created_at = str(row["created_at"] or "")
        parsed_created_at = _parse_created_at(created_at)
        if parsed_created_at and parsed_created_at < cutoff:
            continue
        tickers = _parse_json_list(row["tickers"])
        signal = NewsSignal(
            id=int(row["id"] or 0),
            url=str(row["url"] or ""),
            topic_name=str(row["topic_name"] or ""),
            title=str(row["title_cn"] or ""),
            summary=str(row["summary"] or ""),
            so_what=str(row["so_what"] or ""),
            impact_score=int(row["impact_score"] or 0),
            tickers=tickers,
            normalized_tickers=[code for code in (normalize_ticker(ticker) for ticker in tickers) if code],
            affected_markets=_parse_json_list(row["affected_markets"]),
            asset_classes=_parse_json_list(row["asset_classes"]),
            direction=str(row["direction"] or ""),
            horizon=str(row["horizon"] or ""),
            confidence=float(row["confidence"] or 0),
            created_at=created_at,
        )
        dedupe_title = signal.title.strip().lower()
        if signal.url and signal.url in seen_urls:
            continue
        if dedupe_title and dedupe_title in seen_titles:
            continue
        keep, ranked, score = _rank_signal(signal, candidate_code_set, focus_code_set, max(config.min_impact, 0))
        if not keep:
            continue
        if signal.url:
            seen_urls.add(signal.url)
        if dedupe_title:
            seen_titles.add(dedupe_title)
        ranked_signals.append((score, ranked))

    ranked_signals.sort(key=lambda item: item[0], reverse=True)
    signals = [signal for _, signal in ranked_signals[: max(config.max_signals, 1)]]

    notes = [format_signal_note(signal) for signal in signals]
    return {
        "ok": True,
        "enabled": True,
        "db_path": str(db_path),
        "lookback_hours": config.lookback_hours,
        "min_impact": config.min_impact,
        "candidate_codes": sorted(candidate_code_set),
        "focus_codes": sorted(focus_code_set),
        "count": len(signals),
        "signals": [signal.to_dict() for signal in signals],
        "notes": notes,
    }


def format_signal_note(signal: NewsSignal) -> str:
    tickers = ", ".join(signal.tickers) if signal.tickers else "无明确标的"
    markets = ", ".join(signal.affected_markets) if signal.affected_markets else "未知市场"
    assets = ", ".join(signal.asset_classes) if signal.asset_classes else "未知资产"
    match = signal.match_type
    if signal.matched_codes:
        match = f"{match}:{','.join(signal.matched_codes)}"
    parts = [
        f"[autoNews] match={match} impact={signal.impact_score} confidence={signal.confidence:.2f}",
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
