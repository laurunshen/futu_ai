from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, PROJECT_ROOT
from .futu_client import FutuPaperClient
from .gemini_engine import GeminiDecisionEngine, GeminiTradeDecision
from .models import OrderIntent, infer_market
from .news_signals import load_news_signals
from .portfolios import apply_order_to_portfolio, load_portfolios
from .watchlist import WatchItem, load_watchlist


LOG_DIR = PROJECT_ROOT / "data" / "decisions"


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
        watchlist = load_watchlist(markets=self.config.gemini.observe_markets)
        snapshots = self._snapshot_watchlist(watchlist)
        watch_codes = [item.code for item in watchlist]
        candidates = self._select_candidates(snapshots, limit=self.config.gemini.candidate_count)
        account = self._account_summary("US")
        positions = self._positions_all()
        news_payload = load_news_signals(
            self.config.news,
            focus_codes=watch_codes,
            candidate_codes=[str(candidate.get("code")) for candidate in candidates],
        )
        news_boosts = self._news_candidate_boosts(news_payload, watch_codes)
        if news_boosts:
            candidates = self._select_candidates(
                snapshots,
                limit=self.config.gemini.candidate_count,
                priority_scores=news_boosts,
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
        )
        log_path = self._append_log(result)
        return AutoTradeResult(**{**asdict(result), "log_path": str(log_path)})

    def run_portfolios_once(self, execute: bool | None = None, notes: list[str] | None = None) -> dict[str, Any]:
        store = load_portfolios()
        results = []
        for portfolio in store.get("portfolios", []):
            results.append(self._run_for_portfolio(portfolio, notes=notes or []))
        return {
            "ok": all(result.ok for result in results) if results else True,
            "mode": "portfolio_multi_decision",
            "count": len(results),
            "execute_requested": bool(execute),
            "results": [_clean(asdict(result)) for result in results],
        }

    def _run_for_portfolio(self, portfolio: dict[str, Any], notes: list[str]) -> AutoTradeResult:
        positions_raw = [dict(position) for position in portfolio.get("positions", []) if isinstance(position, dict)]
        portfolio_codes = [str(position.get("code", "")).upper() for position in positions_raw if position.get("code")]
        watchlist = self._watchlist_with_portfolio_codes(portfolio_codes, positions_raw)
        snapshots = self._snapshot_watchlist(watchlist)
        watch_codes = [item.code for item in watchlist]
        priority_scores = {code: 1500.0 for code in portfolio_codes}
        candidates = self._select_candidates(
            snapshots,
            limit=max(self.config.gemini.candidate_count, min(len(portfolio_codes) + 3, 12)),
            priority_scores=priority_scores,
        )
        snapshot_by_code = {str(row.get("code", "")).upper(): row for row in snapshots}
        positions = self._enrich_portfolio_positions(positions_raw, snapshot_by_code)
        fx_payload = self.client.fx_rates_to_hkd()
        account = {
            "type": "local_portfolio",
            "id": portfolio.get("id"),
            "name": portfolio.get("name"),
            "base_currency": portfolio.get("base_currency"),
            "cash": portfolio.get("cash", 0),
            "cash_by_currency": portfolio.get("cash_by_currency", {}),
            "fx_to_hkd": fx_payload.get("fx_to_hkd") or portfolio.get("fx_to_hkd", {}),
            "fx_source": fx_payload.get("source"),
            "fx_ok": bool(fx_payload.get("ok")),
            "fx_error": fx_payload.get("error"),
            "buying_power_rule": "Local ledger can auto-convert base currency cash for cross-currency simulated buys; FX uses Futu OpenD if available, otherwise local defaults are explicitly recorded.",
            "apply_mode": portfolio.get("apply_mode", "manual"),
            "position_count": len(positions),
            "price_rule": "Current prices are only from Futu OpenD snapshots attached to positions/candidates.",
        }
        news_payload = load_news_signals(
            self.config.news,
            focus_codes=watch_codes,
            candidate_codes=[str(candidate.get("code")) for candidate in candidates],
        )
        news_boosts = self._news_candidate_boosts(news_payload, watch_codes)
        if news_boosts:
            for code in portfolio_codes:
                news_boosts[code] = max(news_boosts.get(code, 0), 1500.0)
            candidates = self._select_candidates(
                snapshots,
                limit=max(self.config.gemini.candidate_count, min(len(portfolio_codes) + 3, 12)),
                priority_scores=news_boosts,
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
        news_notes.append(f"本轮是本地模拟盘决策；当前模拟盘应用模式={apply_mode}；不会提交富途订单。")
        news_notes.append(
            f"FX口径：{fx_payload.get('source')}；"
            f"{'已使用富途OpenD FX快照' if fx_payload.get('ok') else '富途FX不可用，使用本地默认汇率'}。"
        )

        decision = self.engine.decide(candidates=candidates, positions=positions, account=account, notes=news_notes)
        order, blocked = self._build_order(decision, positions)
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
                "base_currency": portfolio.get("base_currency"),
                "cash": portfolio.get("cash", 0),
                "cash_by_currency": portfolio.get("cash_by_currency", {}),
                "fx_to_hkd": fx_payload.get("fx_to_hkd") or portfolio.get("fx_to_hkd", {}),
                "fx_source": fx_payload.get("source"),
                "fx_ok": bool(fx_payload.get("ok")),
                "fx_error": fx_payload.get("error"),
                "apply_mode": apply_mode,
                "position_count": len(positions),
            },
        )
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
            applied = apply_order_to_portfolio(
                str(portfolio.get("id") or ""),
                order,
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
                result = self.run_portfolios_once(execute=execute)
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
        self, portfolio_codes: list[str], positions: list[dict[str, Any]]
    ) -> list[WatchItem]:
        items = load_watchlist(markets=self.config.gemini.observe_markets)
        by_code = {item.code: item for item in items}
        position_by_code = {str(position.get("code", "")).upper(): position for position in positions}
        for code in portfolio_codes:
            if code in by_code:
                continue
            try:
                market = infer_market(code)
            except ValueError:
                continue
            if market not in self.config.gemini.observe_markets:
                continue
            position = position_by_code.get(code, {})
            by_code[code] = WatchItem(
                code=code,
                name=str(position.get("name") or code),
                sector=str(position.get("note") or "Portfolio"),
                market=market,
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
            row["market"] = item.market
            enriched.append(_clean(row))
        return enriched

    def _select_candidates(
        self,
        rows: list[dict[str, Any]],
        limit: int,
        priority_scores: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        scored = []
        priority_scores = priority_scores or {}
        for row in rows:
            code = str(row.get("code", "")).upper()
            last_price = self._num(row.get("last_price"))
            prev_close = self._num(row.get("prev_close_price"))
            turnover = self._num(row.get("turnover"))
            amplitude = self._num(row.get("amplitude"))
            volume_ratio = self._num(row.get("volume_ratio"))
            if last_price <= 0 or prev_close <= 0:
                continue
            change_pct = (last_price - prev_close) / prev_close * 100
            score = abs(change_pct) * 2.8 + amplitude * 0.8 + max(volume_ratio - 1, 0) * 4
            if turnover > 0:
                score += min(math.log10(turnover), 12) * 0.35
            news_boost = priority_scores.get(code, 0)
            if news_boost:
                score += 500 + news_boost * 5
            scored.append(
                {
                    "code": code or row.get("code"),
                    "name": row.get("name") or row.get("watch_name"),
                    "sector": row.get("watch_sector"),
                    "market": row.get("market"),
                    "last_price": last_price,
                    "bid_price": self._num(row.get("bid_price")),
                    "ask_price": self._num(row.get("ask_price")),
                    "prev_close_price": prev_close,
                    "change_pct": round(change_pct, 3),
                    "amplitude": amplitude,
                    "volume_ratio": volume_ratio,
                    "turnover": turnover,
                    "lot_size": self._num(row.get("lot_size")) or 1,
                    "score": round(score, 3),
                    "news_boost": round(news_boost, 3),
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

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
        self, decision: GeminiTradeDecision, positions: list[dict[str, Any]]
    ) -> tuple[OrderIntent | None, list[str]]:
        blocked: list[str] = []
        if decision.action == "HOLD":
            return None, []
        code = decision.code.upper()
        try:
            market = infer_market(code)
        except ValueError as exc:
            return None, [str(exc)]

        if market not in self.config.gemini.execute_markets:
            blocked.append(f"{market} is observe-only. GEMINI_EXECUTE_MARKETS={sorted(self.config.gemini.execute_markets)}")
        if decision.confidence < self.config.gemini.confidence_threshold:
            blocked.append(
                f"confidence {decision.confidence} is below threshold {self.config.gemini.confidence_threshold}"
            )
        if not self._trade_quota_available():
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

        max_notional = min(
            decision.max_notional or self.config.gemini.max_notional.get(market, 0),
            self.config.gemini.max_notional.get(market, 0),
            self.config.risk.max_order_value.get(market, 0),
        )
        qty = math.floor(max_notional / price)
        lot_size = int(self._num(row.get("lot_size")) or 1)
        if market == "HK" and lot_size > 1:
            qty = (qty // lot_size) * lot_size
        if qty <= 0:
            blocked.append(f"max_notional {max_notional} is too small for price {price}")
            return None, blocked

        if decision.action == "SELL":
            held_qty = self._held_qty(code, positions)
            if held_qty <= 0:
                blocked.append("SELL blocked because there is no long position")
            qty = min(qty, int(held_qty))
            if qty <= 0:
                return None, blocked

        intent = OrderIntent(
            code=code,
            side=decision.action,
            qty=qty,
            price=round(price, 4),
            order_type="NORMAL",
            reason=f"Gemini: {decision.reason[:120]}",
        )
        risk_decision = self.client.validate(intent)
        blocked.extend(risk_decision.violations)
        return intent, blocked

    def _trade_quota_available(self) -> bool:
        today = datetime.now().date().isoformat()
        log_path = LOG_DIR / f"{today}.jsonl"
        if not log_path.exists():
            return True
        executed = 0
        last_ts: datetime | None = None
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("execution", {}).get("mode") == "paper_execute" and item.get("execution", {}).get("ok"):
                executed += 1
                ts_raw = item.get("timestamp")
                if ts_raw:
                    last_ts = datetime.fromisoformat(ts_raw)
        if executed >= self.config.gemini.max_trades_per_day:
            return False
        if last_ts:
            elapsed = datetime.now(timezone.utc) - last_ts
            if elapsed.total_seconds() < self.config.gemini.cooldown_minutes * 60:
                return False
        return True

    def _append_log(self, result: AutoTradeResult) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{datetime.now().date().isoformat()}.jsonl"
        payload = _clean(asdict(result))
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        payload.pop("log_path", None)
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
