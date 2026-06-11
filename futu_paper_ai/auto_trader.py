from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, PROJECT_ROOT
from .evaluation import build_decision_review_baseline
from .futu_client import FutuPaperClient
from .futu_sync import apply_order_with_optional_futu_sync
from .gemini_engine import GeminiDecisionEngine, GeminiTradeDecision
from .market_data import auto_loop_session, extended_session_from_quote, market_session_payload
from .models import OrderIntent, infer_market
from .news_signals import load_news_signals
from .portfolios import effective_fx_payload, load_portfolios
from .risk import RiskEngine, risk_config_to_payload, risk_config_with_overrides
from .storage import file_lock
from .watchlist import WatchItem, load_watchlist


LOG_DIR = PROJECT_ROOT / "data" / "decisions"
ABNORMAL_CHANGE_PCT = 4.0
ABNORMAL_VOLUME_RATIO = 2.0
ABNORMAL_AMPLITUDE_PCT = 6.0
ABNORMAL_EXTENDED_CHANGE_PCT = 3.0


def _portfolio_kind_label(kind: Any) -> str:
    return "实际仓位镜像" if str(kind or "").lower() == "actual" else "模拟实验盘"


@dataclass(frozen=True)
class AutoTradeResult:
    ok: bool
    mode: str
    decision_id: str
    decision: dict[str, Any]
    gemini_usage: dict[str, Any]
    news_notes: list[str]
    news_signals: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    order: dict[str, Any] | None
    execution: dict[str, Any] | None
    application: dict[str, Any] | None
    blocked_reasons: list[str]
    log_path: str
    source: str = "futu_simulate"
    portfolio: dict[str, Any] | None = None
    review: dict[str, Any] | None = None


def _clean(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if hasattr(value, "item"):
        return _clean(value.item())
    return str(value)


class AutoTrader:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client = FutuPaperClient(config)
        self.engine = GeminiDecisionEngine(config.gemini)

    def run_once(self, execute: bool | None = None, notes: list[str] | None = None) -> AutoTradeResult:
        execute = self.config.gemini.auto_execute if execute is None else execute
        market_schedule = auto_loop_session(self.config.gemini.observe_markets)
        scan_markets = set(market_schedule["scan_markets"] or self.config.gemini.observe_markets)
        watchlist = load_watchlist(markets=scan_markets)
        snapshots = self._snapshot_watchlist(watchlist)
        watch_codes = [item.code for item in watchlist]
        trigger_scores, trigger_reasons = self._abnormal_candidate_boosts(snapshots)
        candidates = self._select_candidates(
            snapshots,
            limit=self.config.gemini.candidate_count,
            priority_scores=trigger_scores,
            priority_reasons=trigger_reasons,
        )
        account = self._account_summary("US")
        account["market_schedule"] = market_schedule
        account["market_anchors"] = self._market_anchor_rows(snapshots)
        positions = self._positions_all()
        news_payload = load_news_signals(
            self.config.news,
            focus_codes=watch_codes,
            candidate_codes=[str(candidate.get("code")) for candidate in candidates],
        )
        news_boosts = self._news_candidate_boosts(news_payload, watch_codes)
        if news_boosts:
            combined_scores, combined_reasons = self._merge_priority_inputs(
                (trigger_scores, trigger_reasons),
                (news_boosts, {code: ["高影响新闻命中"] for code in news_boosts}),
            )
            candidates = self._select_candidates(
                snapshots,
                limit=self.config.gemini.candidate_count,
                priority_scores=combined_scores,
                priority_reasons=combined_reasons,
                news_scores=news_boosts,
            )
            news_payload = load_news_signals(
                self.config.news,
                focus_codes=watch_codes,
                candidate_codes=[str(candidate.get("code")) for candidate in candidates],
            )
        news_notes = [str(note) for note in (notes or [])]
        if news_payload.get("ok"):
            news_notes.extend(str(note) for note in news_payload.get("notes") or [])

        decision = self.engine.decide(candidates=candidates, positions=positions, account=account, notes=news_notes)
        order, blocked = self._build_order(decision, positions)
        execution = None

        if order and execute and not blocked:
            execution = self.client.place_order(order, execute=True)
        elif order:
            execution = self.client.place_order(order, execute=False)

        result = AutoTradeResult(
            ok=not blocked and (execution is None or bool(execution.get("ok"))),
            mode="execute" if execute else "dry_run",
            decision_id=self._new_decision_id(),
            decision=decision.to_dict(),
            gemini_usage=self.engine.last_usage,
            news_notes=news_notes,
            news_signals=[dict(signal) for signal in (news_payload.get("signals") or [])],
            candidates=candidates,
            order=order.to_dict() if order else None,
            execution=execution,
            application=None,
            blocked_reasons=blocked,
            log_path="",
            review=None,
        )
        log_path = self._append_log(result)
        return AutoTradeResult(**{**asdict(result), "log_path": str(log_path)})

    def run_portfolios_once(self, execute: bool | None = None, notes: list[str] | None = None) -> dict[str, Any]:
        store = load_portfolios()
        market_schedule = auto_loop_session(self.config.gemini.observe_markets)
        scan_markets = set(market_schedule["scan_markets"] or self.config.gemini.observe_markets)
        results = []
        skipped = []
        for portfolio in store.get("portfolios", []):
            if not bool(portfolio.get("ai_loop_enabled", True)):
                skipped.append(
                    {
                        "id": portfolio.get("id"),
                        "name": portfolio.get("name"),
                        "reason": "ai_loop_enabled=false",
                    }
                )
                continue
            results.append(
                self._run_for_portfolio(
                    portfolio,
                    notes=notes or [],
                    market_schedule=market_schedule,
                    scan_markets=scan_markets,
                )
            )
        return {
            "ok": all(result.ok for result in results) if results else True,
            "mode": "portfolio_multi_decision",
            "count": len(results),
            "skipped_count": len(skipped),
            "skipped_portfolios": skipped,
            "execute_requested": bool(execute),
            "market_schedule": market_schedule,
            "results": [_clean(asdict(result)) for result in results],
        }

    def _run_for_portfolio(
        self,
        portfolio: dict[str, Any],
        notes: list[str],
        *,
        market_schedule: dict[str, Any] | None = None,
        scan_markets: set[str] | None = None,
    ) -> AutoTradeResult:
        market_schedule = market_schedule or auto_loop_session(self.config.gemini.observe_markets)
        scan_markets = set(scan_markets or market_schedule.get("scan_markets") or self.config.gemini.observe_markets)
        positions_raw = [dict(position) for position in portfolio.get("positions", []) if isinstance(position, dict)]
        portfolio_codes = [str(position.get("code", "")).upper() for position in positions_raw if position.get("code")]
        watchlist = self._watchlist_with_portfolio_codes(portfolio_codes, positions_raw, markets=scan_markets)
        snapshots = self._snapshot_watchlist(watchlist)
        watch_codes = [item.code for item in watchlist]
        trigger_scores, trigger_reasons = self._abnormal_candidate_boosts(snapshots)
        holding_scores = {code: 1500.0 for code in portfolio_codes}
        holding_reasons = {code: ["已有持仓，必须进入复盘候选"] for code in portfolio_codes}
        priority_scores, priority_reasons = self._merge_priority_inputs(
            (trigger_scores, trigger_reasons),
            (holding_scores, holding_reasons),
        )
        candidates = self._select_candidates(
            snapshots,
            limit=max(self.config.gemini.candidate_count, min(len(portfolio_codes) + 3, 12)),
            priority_scores=priority_scores,
            priority_reasons=priority_reasons,
        )
        snapshot_by_code = {str(row.get("code", "")).upper(): row for row in snapshots}
        positions = self._enrich_portfolio_positions(positions_raw, snapshot_by_code)
        upstream_fx_payload = self.client.fx_rates_to_hkd()
        fx_payload = effective_fx_payload(portfolio, upstream_fx_payload)
        portfolio_kind = str(portfolio.get("portfolio_kind") or "paper").lower()
        portfolio_kind_label = _portfolio_kind_label(portfolio_kind)
        effective_risk = risk_config_with_overrides(self.config.risk, portfolio.get("risk_overrides"))
        cadence_max_trades, cadence_cooldown = self._trade_quota_config(portfolio)
        account = {
            "type": "local_portfolio",
            "id": portfolio.get("id"),
            "name": portfolio.get("name"),
            "portfolio_kind": portfolio_kind,
            "portfolio_kind_label": portfolio_kind_label,
            "base_currency": portfolio.get("base_currency"),
            "cash": portfolio.get("cash", 0),
            "cash_by_currency": portfolio.get("cash_by_currency", {}),
            "fx_to_hkd": fx_payload.get("fx_to_hkd") or portfolio.get("fx_to_hkd", {}),
            "fx_source": fx_payload.get("source"),
            "fx_ok": bool(fx_payload.get("ok")),
            "fx_error": fx_payload.get("error"),
            "buying_power_rule": "本地账本买入跨币种资产时，可按汇率从基础币种现金自动换汇扣款；优先使用富途 OpenD FX，失败时明确记录本地默认汇率来源。",
            "apply_mode": portfolio.get("apply_mode", "manual"),
            "strategy_profile": portfolio.get("strategy_profile", "general"),
            "strategy_tags": list(portfolio.get("strategy_tags", [])),
            "strategy_hypothesis": dict(portfolio.get("strategy_hypothesis") or {}),
            "prompt_template": str(portfolio.get("prompt_template") or ""),
            "risk_overrides": dict(portfolio.get("risk_overrides") or {}),
            "effective_risk": risk_config_to_payload(effective_risk),
            "trade_cadence": {
                "max_trades_per_day": cadence_max_trades,
                "cooldown_minutes": cadence_cooldown,
            },
            "futu_sync_enabled": bool(portfolio.get("futu_sync_enabled")),
            "position_count": len(positions),
            "recent_operations": list(portfolio.get("operations", []))[-20:],
            "market_anchors": self._market_anchor_rows(snapshots),
            "market_schedule": market_schedule,
            "price_rule": "当前价只能来自持仓和候选行情里的富途 OpenD 快照。",
        }
        news_payload = load_news_signals(
            self.config.news,
            focus_codes=watch_codes,
            candidate_codes=[str(candidate.get("code")) for candidate in candidates],
        )
        news_boosts = self._news_candidate_boosts(news_payload, watch_codes)
        if news_boosts:
            news_reasons = {code: ["高影响新闻命中"] for code in news_boosts}
            priority_scores, priority_reasons = self._merge_priority_inputs(
                (trigger_scores, trigger_reasons),
                (holding_scores, holding_reasons),
                (news_boosts, news_reasons),
            )
            candidates = self._select_candidates(
                snapshots,
                limit=max(self.config.gemini.candidate_count, min(len(portfolio_codes) + 3, 12)),
                priority_scores=priority_scores,
                priority_reasons=priority_reasons,
                news_scores=news_boosts,
            )
            news_payload = load_news_signals(
                self.config.news,
                focus_codes=watch_codes,
                candidate_codes=[str(candidate.get("code")) for candidate in candidates],
            )

        news_notes = [str(note) for note in notes]
        if news_payload.get("ok"):
            news_notes.extend(str(note) for note in news_payload.get("notes") or [])
        apply_mode = str(portfolio.get("apply_mode") or "manual").lower()
        futu_sync_text = "开启" if portfolio.get("futu_sync_enabled") else "关闭"
        if portfolio_kind == "actual":
            portfolio_note = "本轮是实际仓位镜像盘决策；这些持仓代表用户真实券商仓位的本地镜像。"
            language_note = "请把它称为实际仓位或实际仓位镜像，不要称为教学模拟盘。"
        else:
            portfolio_note = "本轮是模拟实验盘决策；用于策略实验、AB Test 和复盘。"
            language_note = "可以称为模拟实验盘，但仍按严肃风控处理。"
        news_notes.append(
            f"{portfolio_note}当前应用模式={apply_mode}；富途模拟盘同步={futu_sync_text}。"
            "manual 需要用户确认，auto 会按设置应用；若同步开启，应用时先提交富途模拟单，再按实际成交反写本地。"
        )
        news_notes.append(
            f"策略档案={portfolio.get('strategy_profile', 'general')}；"
            f"策略标签={', '.join(portfolio.get('strategy_tags') or []) or '未设置'}。"
        )
        hypothesis = portfolio.get("strategy_hypothesis") if isinstance(portfolio.get("strategy_hypothesis"), dict) else {}
        if hypothesis:
            news_notes.append(
                "预注册策略假设："
                f"版本={hypothesis.get('version') or '未设置'}；"
                f"基准={hypothesis.get('benchmark') or '未设置'}；"
                f"假设={hypothesis.get('hypothesis') or '未设置'}；"
                f"成功口径={hypothesis.get('success_metric') or '未设置'}。"
            )
        if portfolio.get("risk_overrides"):
            news_notes.append(
                "本组合使用独立风控覆盖："
                f"{json.dumps(risk_config_to_payload(effective_risk), ensure_ascii=False)}。"
                "注意：风控数值 0 表示该项未启用限制/不限额，不表示可用预算或允许仓位为 0。"
            )
        news_notes.append(
            "本组合交易节奏覆盖："
            f"每日最多交易数={cadence_max_trades if cadence_max_trades > 0 else '不限'}；"
            f"冷却分钟={cadence_cooldown if cadence_cooldown > 0 else '不限'}。"
        )
        if portfolio.get("prompt_template"):
            news_notes.append(f"本组合策略提示模板：{str(portfolio.get('prompt_template'))[:1500]}")
        news_notes.append(language_note)
        news_notes.append(
            f"FX口径：{fx_payload.get('source')}；"
            f"{'使用券商校准/实时FX' if fx_payload.get('ok') else '富途FX不可用，使用本地默认汇率'}。"
        )
        news_notes.append(
            "交易时段规则：常规交易时段可应用订单；开盘前准备窗口只扫描和生成开盘计划；"
            "闭市、午休和周末不允许本地模拟成交。当前状态="
            f"{json.dumps(market_schedule, ensure_ascii=False)}。"
        )

        decision = self.engine.decide(candidates=candidates, positions=positions, account=account, notes=news_notes)
        order, blocked = self._build_order(decision, positions, portfolio=portfolio, fx_payload=fx_payload)
        execution = None
        if order:
            execution = {
                "ok": True,
                "mode": "portfolio_suggestion",
                "message": "Local portfolio decision only; no Futu order submitted.",
                "intent": order.to_dict(),
            }
        decision_id = self._new_decision_id()
        application = self._portfolio_application(
            portfolio=portfolio,
            order=order.to_dict() if order else None,
            blocked=blocked,
            decision_id=decision_id,
            reason=decision.reason,
            fx_payload=fx_payload,
        )

        result = AutoTradeResult(
            ok=not blocked and (application is None or bool(application.get("ok", True))),
            mode="portfolio_decision",
            decision_id=decision_id,
            decision=decision.to_dict(),
            gemini_usage=self.engine.last_usage,
            news_notes=news_notes,
            news_signals=[dict(signal) for signal in (news_payload.get("signals") or [])],
            candidates=candidates,
            order=order.to_dict() if order else None,
            execution=execution,
            application=application,
            blocked_reasons=blocked,
            log_path="",
            source="local_portfolio",
            portfolio={
                "id": portfolio.get("id"),
                "name": portfolio.get("name"),
                "portfolio_kind": portfolio_kind,
                "portfolio_kind_label": portfolio_kind_label,
                "base_currency": portfolio.get("base_currency"),
                "cash": portfolio.get("cash", 0),
                "cash_by_currency": portfolio.get("cash_by_currency", {}),
                "fx_to_hkd": fx_payload.get("fx_to_hkd") or portfolio.get("fx_to_hkd", {}),
                "fx_source": fx_payload.get("source"),
                "fx_ok": bool(fx_payload.get("ok")),
                "fx_error": fx_payload.get("error"),
                "apply_mode": apply_mode,
                "strategy_profile": portfolio.get("strategy_profile", "general"),
                "strategy_tags": list(portfolio.get("strategy_tags", [])),
                "strategy_hypothesis": dict(portfolio.get("strategy_hypothesis") or {}),
                "prompt_template": str(portfolio.get("prompt_template") or ""),
                "risk_overrides": dict(portfolio.get("risk_overrides") or {}),
                "effective_risk": risk_config_to_payload(effective_risk),
                "trade_cadence": {
                    "max_trades_per_day": cadence_max_trades,
                    "cooldown_minutes": cadence_cooldown,
                },
                "futu_sync_enabled": bool(portfolio.get("futu_sync_enabled")),
                "position_count": len(positions),
            },
        )
        review = build_decision_review_baseline(
            portfolio=portfolio,
            positions=positions,
            candidates=candidates,
            decision=decision.to_dict(),
            order=order.to_dict() if order else None,
            fx_payload=fx_payload,
            news_signals=[dict(signal) for signal in (news_payload.get("signals") or [])],
        )
        result = AutoTradeResult(**{**asdict(result), "review": review})
        log_path = self._append_log(result)
        return AutoTradeResult(**{**asdict(result), "log_path": str(log_path)})

    def _new_decision_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _portfolio_application(
        self,
        *,
        portfolio: dict[str, Any],
        order: dict[str, Any] | None,
        blocked: list[str],
        decision_id: str,
        reason: str,
        fx_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        mode = str(portfolio.get("apply_mode") or "manual").strip().lower()
        if mode not in {"observe", "manual", "auto"}:
            mode = "manual"
        if not order:
            return {"ok": True, "status": "not_applicable", "mode": mode, "message": "No local order generated."}
        if blocked:
            return {
                "ok": False,
                "status": "blocked",
                "mode": mode,
                "message": "; ".join(blocked),
            }
        if mode == "observe":
            return {
                "ok": True,
                "status": "skipped",
                "mode": mode,
                "message": "Observe-only portfolio; decision was not applied.",
            }
        if mode == "manual":
            return {
                "ok": True,
                "status": "pending",
                "mode": mode,
                "message": "Manual portfolio; waiting for user approval.",
            }
        try:
            applied = apply_order_with_optional_futu_sync(
                client=self.client,
                portfolio=portfolio,
                portfolio_id=str(portfolio.get("id") or ""),
                order_payload=order,
                source="auto",
                decision_id=decision_id,
                reason=reason,
                fx_to_hkd=(fx_payload or {}).get("fx_to_hkd"),
                fx_source=str((fx_payload or {}).get("source") or ""),
                fx_status=fx_payload,
            )
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "mode": mode,
                "message": str(exc),
            }
        return {**applied, "mode": mode}

    def loop(self, execute: bool | None = None) -> None:
        interval = max(30, self.config.gemini.loop_interval_seconds)
        print(f"Gemini auto loop started. interval={interval}s execute={execute}")
        while True:
            try:
                self.config = AppConfig.from_env()
                self.client = FutuPaperClient(self.config)
                self.engine = GeminiDecisionEngine(self.config.gemini)
                interval = max(30, self.config.gemini.loop_interval_seconds)
                schedule = auto_loop_session(self.config.gemini.observe_markets)
                if not schedule["should_scan"]:
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "mode": "market_schedule_skip",
                                "interval_seconds": interval,
                                "market_schedule": schedule,
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                    time.sleep(interval)
                    continue
                result = self.run_portfolios_once(execute=execute)
                result["market_schedule"] = schedule
                print(json.dumps(_clean(result), ensure_ascii=False, default=str))
            except Exception as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            time.sleep(interval)

    def _snapshot_watchlist(self, watchlist: list[WatchItem]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        code_to_item = {item.code: item for item in watchlist}
        batch_size = 30
        codes = list(code_to_item)
        for start in range(0, len(codes), batch_size):
            batch = codes[start : start + batch_size]
            payload = self.client.snapshot(batch)
            if payload.get("ok"):
                rows.extend(self._attach_watch_metadata(payload.get("data", []), code_to_item))
                continue
            for code in batch:
                single = self.client.snapshot([code])
                if single.get("ok"):
                    rows.extend(self._attach_watch_metadata(single.get("data", []), code_to_item))
        return rows

    def _watchlist_with_portfolio_codes(
        self,
        portfolio_codes: list[str],
        positions: list[dict[str, Any]],
        *,
        markets: set[str] | None = None,
    ) -> list[WatchItem]:
        market_filter = set(markets or self.config.gemini.observe_markets)
        items = load_watchlist(markets=market_filter)
        by_code = {item.code: item for item in items}
        position_by_code = {str(position.get("code", "")).upper(): position for position in positions}
        for code in portfolio_codes:
            if code in by_code:
                continue
            try:
                market = infer_market(code)
            except ValueError:
                continue
            if market not in market_filter:
                continue
            position = position_by_code.get(code, {})
            by_code[code] = WatchItem(
                code=code,
                name=str(position.get("name") or code),
                sector=str(position.get("note") or "持仓"),
                market=market,
                tier="holding",
                role="trade",
            )
        return sorted(by_code.values(), key=lambda item: item.code)

    def _enrich_portfolio_positions(
        self, positions: list[dict[str, Any]], snapshot_by_code: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        enriched = []
        for position in positions:
            row = dict(position)
            code = str(row.get("code", "")).upper()
            snapshot = snapshot_by_code.get(code, {})
            last_price = self._num(snapshot.get("last_price") or snapshot.get("bid_price") or snapshot.get("ask_price"))
            qty = self._num(row.get("qty"))
            cost_price = self._num(row.get("cost_price"))
            cost_value = qty * cost_price
            row["last_price"] = last_price if last_price > 0 else None
            row["price_source"] = "Futu OpenD snapshot" if last_price > 0 else ""
            row["quote_update_time"] = snapshot.get("update_time")
            row["bid_price"] = self._num(snapshot.get("bid_price")) or None
            row["ask_price"] = self._num(snapshot.get("ask_price")) or None
            row["extended_session"] = snapshot.get("extended_session") or extended_session_from_quote(snapshot, row.get("market", ""))
            row["market_value"] = round(qty * last_price, 4) if last_price > 0 else None
            row["pl_value"] = round(qty * last_price - cost_value, 4) if last_price > 0 else None
            row["pl_ratio"] = round((last_price - cost_price) / cost_price * 100, 4) if last_price > 0 and cost_price > 0 else None
            enriched.append(_clean(row))
        return enriched

    def _attach_watch_metadata(
        self, rows: list[dict[str, Any]], code_to_item: dict[str, WatchItem]
    ) -> list[dict[str, Any]]:
        enriched = []
        for row in rows:
            item = code_to_item.get(str(row.get("code", "")).upper())
            if not item:
                continue
            row = dict(row)
            row["watch_name"] = item.name
            row["watch_sector"] = item.sector
            row["watch_tier"] = item.tier
            row["watch_role"] = item.role
            row["market"] = item.market
            row["extended_session"] = row.get("extended_session") or extended_session_from_quote(row, item.market)
            enriched.append(_clean(row))
        return enriched

    def _market_anchor_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        anchors: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("watch_role") or "").lower() != "context":
                continue
            last_price = self._num(row.get("last_price"))
            prev_close = self._num(row.get("prev_close_price"))
            change_pct = (last_price - prev_close) / prev_close * 100 if last_price > 0 and prev_close > 0 else None
            anchors.append(
                {
                    "code": str(row.get("code") or "").upper(),
                    "name": row.get("name") or row.get("watch_name"),
                    "sector": row.get("watch_sector"),
                    "tier": row.get("watch_tier"),
                    "last_price": last_price or None,
                    "change_pct": round(change_pct, 3) if change_pct is not None else None,
                    "extended_session": row.get("extended_session"),
                }
            )
        return anchors[:10]

    def _select_candidates(
        self,
        rows: list[dict[str, Any]],
        limit: int,
        priority_scores: dict[str, float] | None = None,
        priority_reasons: dict[str, list[str]] | None = None,
        news_scores: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        scored = []
        priority_scores = priority_scores or {}
        priority_reasons = priority_reasons or {}
        news_scores = news_scores or {}
        for row in rows:
            role = str(row.get("watch_role") or "trade").lower()
            if role == "context":
                continue
            code = str(row.get("code", "")).upper()
            last_price = self._num(row.get("last_price"))
            prev_close = self._num(row.get("prev_close_price"))
            turnover = self._num(row.get("turnover"))
            volume = self._num(row.get("volume"))
            amplitude = self._num(row.get("amplitude"))
            volume_ratio = self._num(row.get("volume_ratio"))
            if last_price <= 0 or prev_close <= 0:
                continue
            change_pct = (last_price - prev_close) / prev_close * 100
            score = abs(change_pct) * 2.8 + amplitude * 0.8 + max(volume_ratio - 1, 0) * 4
            if turnover > 0:
                score += min(math.log10(turnover), 12) * 0.35
            extended = row.get("extended_session") or extended_session_from_quote(row, row.get("market", ""))
            extended_change = self._num((extended or {}).get("change_rate"))
            extended_volume = self._num((extended or {}).get("volume"))
            if extended_change:
                score += min(abs(extended_change), 8) * 1.2
            if extended_volume > 0:
                score += min(math.log10(extended_volume), 8) * 0.18
            priority_boost = priority_scores.get(code, 0)
            if priority_boost:
                score += 10000 + priority_boost * 10
            news_boost = news_scores.get(code, 0)
            tier = str(row.get("watch_tier") or "opportunity").lower()
            tier_boost = {"holding": 2200, "core": 450, "learning": 160, "opportunity": 0}.get(tier, 0)
            score += tier_boost
            scored.append(
                {
                    "code": code or row.get("code"),
                    "name": row.get("name") or row.get("watch_name"),
                    "sector": row.get("watch_sector"),
                    "tier": tier,
                    "role": role,
                    "market": row.get("market"),
                    "last_price": last_price,
                    "bid_price": self._num(row.get("bid_price")),
                    "ask_price": self._num(row.get("ask_price")),
                    "prev_close_price": prev_close,
                    "change_pct": round(change_pct, 3),
                    "amplitude": amplitude,
                    "volume": volume,
                    "volume_ratio": volume_ratio,
                    "turnover": turnover,
                    "extended_session": extended,
                    "extended_change_pct": round(extended_change, 3) if extended else None,
                    "extended_price": self._num((extended or {}).get("price")) or None,
                    "lot_size": self._num(row.get("lot_size")) or 1,
                    "score": round(score, 3),
                    "priority_score": round(priority_boost, 3),
                    "priority_reasons": list(priority_reasons.get(code, [])),
                    "news_boost": round(news_boost, 3),
                    "forced_by_news": bool(news_boost),
                    "forced_by_trigger": bool(priority_reasons.get(code)),
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def _abnormal_candidate_boosts(self, rows: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, list[str]]]:
        boosts: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for row in rows:
            role = str(row.get("watch_role") or "trade").lower()
            if role == "context":
                continue
            code = str(row.get("code", "")).upper()
            if not code:
                continue
            last_price = self._num(row.get("last_price"))
            prev_close = self._num(row.get("prev_close_price"))
            volume_ratio = self._num(row.get("volume_ratio"))
            amplitude = self._num(row.get("amplitude"))
            extended = row.get("extended_session") or extended_session_from_quote(row, row.get("market", ""))
            extended_change = self._num((extended or {}).get("change_rate"))
            row_reasons: list[str] = []
            score = 0.0
            if last_price > 0 and prev_close > 0:
                change_pct = (last_price - prev_close) / prev_close * 100
                if abs(change_pct) >= ABNORMAL_CHANGE_PCT:
                    row_reasons.append(f"日内涨跌幅 {change_pct:.2f}% 超过 {ABNORMAL_CHANGE_PCT:g}%")
                    score += 700 + min(abs(change_pct), 12) * 20
            if volume_ratio >= ABNORMAL_VOLUME_RATIO:
                row_reasons.append(f"量比 {volume_ratio:.2f} 超过 {ABNORMAL_VOLUME_RATIO:g}")
                score += 550 + min(volume_ratio, 8) * 45
            if amplitude >= ABNORMAL_AMPLITUDE_PCT:
                row_reasons.append(f"振幅 {amplitude:.2f}% 超过 {ABNORMAL_AMPLITUDE_PCT:g}%")
                score += 450 + min(amplitude, 14) * 18
            if abs(extended_change) >= ABNORMAL_EXTENDED_CHANGE_PCT:
                row_reasons.append(f"盘前/盘后变化 {extended_change:.2f}% 超过 {ABNORMAL_EXTENDED_CHANGE_PCT:g}%")
                score += 520 + min(abs(extended_change), 10) * 25
            if row_reasons:
                boosts[code] = max(boosts.get(code, 0.0), score)
                reasons.setdefault(code, []).extend(row_reasons)
        return boosts, reasons

    def _merge_priority_inputs(
        self,
        *items: tuple[dict[str, float], dict[str, list[str]]],
    ) -> tuple[dict[str, float], dict[str, list[str]]]:
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for score_payload, reason_payload in items:
            for code, score in (score_payload or {}).items():
                key = str(code or "").upper()
                if not key:
                    continue
                scores[key] = max(scores.get(key, 0.0), self._num(score))
            for code, row_reasons in (reason_payload or {}).items():
                key = str(code or "").upper()
                if not key:
                    continue
                bucket = reasons.setdefault(key, [])
                for reason in row_reasons or []:
                    text = str(reason or "").strip()
                    if text and text not in bucket:
                        bucket.append(text)
        return scores, reasons

    def _news_candidate_boosts(self, news_payload: dict[str, Any], watch_codes: list[str]) -> dict[str, float]:
        watch_code_set = {code.upper() for code in watch_codes}
        boosts: dict[str, float] = {}
        if not news_payload.get("ok"):
            return boosts
        for signal in news_payload.get("signals") or []:
            impact = self._num(signal.get("impact_score"))
            if impact <= 0:
                continue
            codes = set()
            codes.update(str(code).upper() for code in signal.get("matched_codes") or [])
            codes.update(str(code).upper() for code in signal.get("normalized_tickers") or [])
            for code in codes & watch_code_set:
                boosts[code] = max(boosts.get(code, 0), impact)
        return boosts

    def _account_summary(self, market: str) -> dict[str, Any]:
        currency = "USD" if market == "US" else "HKD"
        payload = self.client.account(market, currency)
        rows = payload.get("data") or []
        return rows[0] if rows else {}

    def _positions_all(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for market in sorted(self.config.gemini.observe_markets):
            payload = self.client.positions(market)
            if payload.get("ok"):
                rows.extend(payload.get("data") or [])
        return _clean(rows)

    def _build_order(
        self,
        decision: GeminiTradeDecision,
        positions: list[dict[str, Any]],
        *,
        portfolio: dict[str, Any] | None = None,
        fx_payload: dict[str, Any] | None = None,
    ) -> tuple[OrderIntent | None, list[str]]:
        blocked: list[str] = []
        if decision.action == "HOLD":
            return None, []
        code = decision.code.upper()
        try:
            market = infer_market(code)
        except ValueError as exc:
            return None, [str(exc)]

        session = market_session_payload(market)
        if not session["can_trade"]:
            blocked.append(
                f"{market} market is {session['status']}; AI local order applications are blocked outside regular "
                f"trading sessions ({session['local_time']} {session['timezone']}: {session['reason']})"
            )
        if market not in self.config.gemini.execute_markets:
            blocked.append(f"{market} is observe-only. GEMINI_EXECUTE_MARKETS={sorted(self.config.gemini.execute_markets)}")
        if decision.confidence < self.config.gemini.confidence_threshold:
            blocked.append(
                f"confidence {decision.confidence} is below threshold {self.config.gemini.confidence_threshold}"
            )
        if not self._trade_quota_available(portfolio=portfolio):
            blocked.append("daily trade quota or cooldown blocked this order")

        quote = self.client.snapshot([code])
        if not quote.get("ok") or not quote.get("data"):
            blocked.append("could not refresh quote for selected code")
            return None, blocked
        row = quote["data"][0]
        price = self._num(row.get("ask_price") if decision.action == "BUY" else row.get("bid_price"))
        if price <= 0:
            price = self._num(row.get("last_price"))
        if price <= 0:
            blocked.append("selected code has no usable price")
            return None, blocked

        effective_risk = risk_config_with_overrides(self.config.risk, portfolio.get("risk_overrides")) if portfolio else self.config.risk
        risk_order_cap = self._num(effective_risk.max_order_value.get(market))
        if decision.action == "SELL":
            held_qty = self._held_qty(code, positions)
            if held_qty <= 0:
                blocked.append("SELL blocked because there is no long position")
                return None, blocked
            max_qty = self._num(effective_risk.max_qty.get(market))
            qty = int(held_qty)
            if max_qty > 0:
                qty = min(qty, int(max_qty))
        else:
            caps = [risk_order_cap]
            decision_cap = self._num(decision.max_notional)
            if decision_cap > 0:
                caps.append(decision_cap)
            if portfolio is not None:
                positive_caps = [cap for cap in caps if cap > 0]
                if not positive_caps:
                    blocked.append(
                        "BUY blocked because decision.max_notional is 0 and no positive budget cap is configured; "
                        "with unlimited caps, BUY decisions must provide a positive order amount"
                    )
                    return None, blocked
                max_notional = min(positive_caps)
            else:
                gemini_cap = self._num(self.config.gemini.max_notional.get(market))
                if gemini_cap > 0:
                    caps.append(gemini_cap)
                positive_caps = [cap for cap in caps if cap > 0]
                if not positive_caps:
                    blocked.append(
                        "BUY blocked because decision.max_notional is 0 and no positive budget cap is configured; "
                        "with unlimited caps, BUY decisions must provide a positive order amount"
                    )
                    return None, blocked
                max_notional = min(positive_caps)
            qty = math.floor(max_notional / price)
        lot_size = int(self._num(row.get("lot_size")) or 1)
        if market == "HK" and lot_size > 1:
            qty = (qty // lot_size) * lot_size
        if qty <= 0:
            if decision.action == "SELL":
                blocked.append(f"SELL blocked because holding or max_qty is too small for lot size {lot_size}")
            else:
                blocked.append(f"max_notional {max_notional} is too small for price {price}")
            return None, blocked

        intent = OrderIntent(
            code=code,
            side=decision.action,
            qty=qty,
            price=round(price, 4),
            order_type="NORMAL",
            reason=f"Gemini: {decision.reason[:120]}",
        )
        if portfolio is not None:
            risk_decision = RiskEngine(effective_risk).validate_portfolio(
                intent,
                portfolio,
                positions=positions,
                fx_to_hkd=(fx_payload or {}).get("fx_to_hkd") if isinstance(fx_payload, dict) else None,
            )
        else:
            risk_decision = self.client.validate(intent)
        blocked.extend(risk_decision.violations)
        return intent, blocked

    def _trade_quota_config(self, portfolio: dict[str, Any] | None = None) -> tuple[int, int]:
        max_trades = int(self.config.gemini.max_trades_per_day)
        cooldown = int(self.config.gemini.cooldown_minutes)
        overrides = portfolio.get("risk_overrides") if isinstance(portfolio, dict) else {}
        if isinstance(overrides, dict):
            if "max_trades_per_day" in overrides:
                max_trades = max(0, int(self._num(overrides.get("max_trades_per_day"))))
            if "cooldown_minutes" in overrides:
                cooldown = max(0, int(self._num(overrides.get("cooldown_minutes"))))
        return max_trades, cooldown

    def _trade_quota_available(self, *, portfolio: dict[str, Any] | None = None) -> bool:
        max_trades_per_day, cooldown_minutes = self._trade_quota_config(portfolio)
        if max_trades_per_day <= 0 and cooldown_minutes <= 0:
            return True
        portfolio_id = str((portfolio or {}).get("id") or "")
        now_utc = datetime.now(timezone.utc)
        today = datetime.now().date()
        lookback_days = max(0, math.ceil(max(cooldown_minutes, 0) / 1440))
        log_paths = [LOG_DIR / f"{(today - timedelta(days=offset)).isoformat()}.jsonl" for offset in range(lookback_days + 1)]
        executed = 0
        last_ts: datetime | None = None
        for log_path in log_paths:
            with file_lock(log_path, exclusive=False):
                if not log_path.exists():
                    continue
                lines = log_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not self._log_item_counts_for_quota(item, portfolio_id=portfolio_id):
                    continue
                ts_raw = item.get("timestamp")
                parsed_ts = None
                if ts_raw:
                    try:
                        parsed_ts = datetime.fromisoformat(str(ts_raw))
                    except ValueError:
                        parsed_ts = None
                    if parsed_ts and parsed_ts.tzinfo is None:
                        parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                    if parsed_ts and (last_ts is None or parsed_ts > last_ts):
                        last_ts = parsed_ts
                if log_path.name == f"{today.isoformat()}.jsonl":
                    executed += 1
        if max_trades_per_day > 0 and executed >= max_trades_per_day:
            return False
        if cooldown_minutes > 0 and last_ts:
            elapsed = now_utc - last_ts
            if elapsed.total_seconds() < cooldown_minutes * 60:
                return False
        return True

    def _log_item_counts_for_quota(self, item: dict[str, Any], *, portfolio_id: str = "") -> bool:
        if portfolio_id:
            portfolio = item.get("portfolio") if isinstance(item.get("portfolio"), dict) else {}
            if str(portfolio.get("id") or "") != portfolio_id:
                return False
        execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
        if execution.get("mode") == "paper_execute" and execution.get("ok"):
            return True
        application = item.get("application") if isinstance(item.get("application"), dict) else {}
        if str(application.get("mode") or "").lower() != "auto":
            return False
        status = str(application.get("status") or "").lower()
        if status not in {"applied", "partially_applied", "futu_submitted"}:
            return False
        return bool(item.get("order"))

    def _append_log(self, result: AutoTradeResult) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{datetime.now().date().isoformat()}.jsonl"
        payload = _clean(asdict(result))
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        payload.pop("log_path", None)
        with file_lock(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path

    def _held_qty(self, code: str, positions: list[dict[str, Any]]) -> float:
        for row in positions:
            if str(row.get("code", "")).upper() == code:
                return self._num(row.get("qty"))
        return 0.0

    def _num(self, value: Any) -> float:
        try:
            if value in {None, "N/A", ""}:
                return 0.0
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                return 0.0
            return number
        except (TypeError, ValueError):
            return 0.0
