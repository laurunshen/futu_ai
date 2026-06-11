from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from futu_paper_ai.portfolios import (
    create_portfolio,
    load_portfolios,
    update_portfolio_cash,
    update_portfolio_settings,
)


START_DATE = "2026-06-12"

SWING_PROMPT = """波段策略盘：做 1-3 个月中线波段，允许集中持仓，优先关注中概互联网与美股科技核心标的。BUY 必须给出明确目标价、失效条件、计划持有周期和 max_notional；证据不足时 HOLD。SELL/减仓必须说明是趋势破坏、估值兑现、风险事件还是换仓机会成本。"""

NEWS_PROMPT = """新闻动量盘：只做高影响新闻或重大异动触发后的交易，不为普通涨跌追单。偏小仓位、快进快出，必须写清新闻催化、价格是否已反映、失效条件和退出计划。没有明确新闻催化或触发器证据不足时必须 HOLD。"""

FREE_PROMPT = """波段自由盘：使用与波段纪律盘相同的 1-3 个月波段判断框架，但不施加交易频率/冷却约束，用来观察更高自由度是否真的提升收益。仍必须给出目标价、失效条件、计划持有周期和 max_notional；不允许因为自由而降低证据标准。"""

HOLD_PROMPT = """对照盘：买入持有基准，用来作为所有主动策略的及格线。建仓后以观察为主，不因短期新闻和噪声频繁换手。"""


def risk(max_hk: int | None, max_us: int | None, max_trades: int, cooldown: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "max_trades_per_day": max_trades,
        "cooldown_minutes": cooldown,
    }
    max_order: dict[str, float] = {}
    if max_hk is not None:
        max_order["HK"] = float(max_hk)
    if max_us is not None:
        max_order["US"] = float(max_us)
    if max_order:
        payload["max_order_value"] = max_order
    return payload


EXPERIMENTS: list[dict[str, Any]] = [
    {
        "name": "A10 对照买入持有",
        "cash": 100_000,
        "apply_mode": "observe",
        "ai_loop_enabled": False,
        "strategy_profile": "long_hold",
        "tags": ["实验矩阵", "10万HKD", "对照盘", "买入持有"],
        "prompt": HOLD_PROMPT,
        "risk": risk(None, None, 0, 0),
        "hypothesis": "A10 是所有主动策略的买入持有及格线；主动盘若长期跑不赢它，说明交易增加了噪声和费用。",
        "success_metric": "作为基准，不追求主动交易；B/C/D 系列以扣费后收益和回撤相对 A10 比较。",
    },
    {
        "name": "B10 新闻动量",
        "cash": 100_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "news_driven",
        "tags": ["实验矩阵", "10万HKD", "新闻动量", "快进快出"],
        "prompt": NEWS_PROMPT,
        "risk": risk(30_000, 4_000, 3, 60),
        "hypothesis": "10万资金下，新闻触发策略可能因一手交易单位和费用拖累而难以稳定跑赢买入持有。",
        "success_metric": "2-4周后比较 B10 vs A10、B10 vs B30 的收益、回撤、交易次数和费用占比。",
    },
    {
        "name": "C10 波段纪律",
        "cash": 100_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "short_swing",
        "tags": ["实验矩阵", "10万HKD", "波段纪律", "真实规模锚点"],
        "prompt": SWING_PROMPT,
        "risk": risk(50_000, 6_500, 1, 2_880),
        "hypothesis": "C10 模拟用户当前真实资金量级，纪律化 1-3 个月波段应减少过度交易，是最能迁移回实盘的镜子。",
        "success_metric": "比较 C10 vs D10 判断纪律是否提升扣费后收益/回撤；比较 C10→C30→C50→C100 判断资金规模影响。",
    },
    {
        "name": "D10 波段自由",
        "cash": 100_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "short_swing",
        "tags": ["实验矩阵", "10万HKD", "波段自由", "不限频率"],
        "prompt": FREE_PROMPT,
        "risk": risk(50_000, 6_500, 0, 0),
        "hypothesis": "D10 检验在 10万规模下完全放开交易频率是否会增加收益，或只是增加费用和噪声。",
        "success_metric": "比较 D10 vs C10 的收益、回撤、交易次数、费用占比和重复加仓行为。",
    },
    {
        "name": "B30 新闻动量",
        "cash": 300_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "news_driven",
        "tags": ["实验矩阵", "30万HKD", "新闻动量", "快进快出"],
        "prompt": NEWS_PROMPT,
        "risk": risk(90_000, 11_500, 3, 60),
        "hypothesis": "30万资金给新闻动量更多分散和仓位试错空间，预期比 B10 更能体现新闻信号价值。",
        "success_metric": "比较 B30 vs B10 验证新闻策略是否对资金规模敏感；同时比较 B30 vs C30/D30。",
    },
    {
        "name": "C30 波段纪律",
        "cash": 300_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "short_swing",
        "tags": ["实验矩阵", "30万HKD", "波段纪律"],
        "prompt": SWING_PROMPT,
        "risk": risk(100_000, 12_800, 1, 2_880),
        "hypothesis": "C30 保持与 C10 相同纪律，只改变资金规模，用来观察一手腾讯等港股交易单位对策略表现的影响是否减弱。",
        "success_metric": "比较 C30 vs C10/C50/C100 的规模梯度，以及 C30 vs D30 的纪律价值。",
    },
    {
        "name": "D30 波段自由",
        "cash": 300_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "short_swing",
        "tags": ["实验矩阵", "30万HKD", "波段自由", "不限频率"],
        "prompt": FREE_PROMPT,
        "risk": risk(150_000, 19_200, 0, 0),
        "hypothesis": "D30 检验更大资金下自由交易是否比 D10 更少受 lot size 与费用拖累，纪律价值是否随资金规模变化。",
        "success_metric": "比较 D30 vs C30，以及 (C10-D10) vs (C30-D30) 的相对表现差异。",
    },
    {
        "name": "C50 波段纪律",
        "cash": 500_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "short_swing",
        "tags": ["实验矩阵", "50万HKD", "波段纪律"],
        "prompt": SWING_PROMPT,
        "risk": risk(150_000, 19_200, 1, 2_880),
        "hypothesis": "C50 是纪律波段的中等资金规模点，用来补齐 C10/C30/C100 之间的规模曲线。",
        "success_metric": "比较 C10→C30→C50→C100 的收益、回撤、成交频率和单笔仓位有效性。",
    },
    {
        "name": "C100 波段纪律",
        "cash": 1_000_000,
        "apply_mode": "auto",
        "ai_loop_enabled": True,
        "strategy_profile": "short_swing",
        "tags": ["实验矩阵", "100万HKD", "波段纪律", "未来资金预演"],
        "prompt": SWING_PROMPT,
        "risk": risk(250_000, 32_000, 1, 2_880),
        "hypothesis": "C100 模拟未来加仓后的资金体量，观察相同纪律在更充分资金规模下是否更容易执行并跑赢基准。",
        "success_metric": "比较 C100 vs C10/C30/C50，评估资金规模增加是否改善策略执行质量。",
    },
]


def _has_activity(portfolio: dict[str, Any]) -> bool:
    if portfolio.get("positions") or portfolio.get("trades") or portfolio.get("futu_sync_orders"):
        return True
    return False


def _upsert_experiment(spec: dict[str, Any]) -> dict[str, Any]:
    store = load_portfolios()
    existing = next((p for p in store.get("portfolios", []) if p.get("name") == spec["name"]), None)
    if existing:
        portfolio_id = existing["id"]
    else:
        store = create_portfolio(spec["name"], base_currency="HKD", cash=spec["cash"])
        portfolio_id = store["active_id"]
        existing = next(p for p in store.get("portfolios", []) if p.get("id") == portfolio_id)

    if existing and not _has_activity(existing):
        update_portfolio_cash(portfolio_id, spec["cash"], currency="HKD")

    hypothesis = {
        "version": "matrix-2026-06-12",
        "benchmark": "A10 对照买入持有；HK.03033 / US.QQQ 作为市场背景",
        "hypothesis": spec["hypothesis"],
        "expected_regime": "中概互联网与美股科技 1-3 个月波段/新闻催化环境。",
        "success_metric": spec["success_metric"],
        "start_date": START_DATE,
        "review_after": "2-4周初评；跨一次明显回调后再做正式结论。",
    }
    store = update_portfolio_settings(
        portfolio_id,
        apply_mode=spec["apply_mode"],
        ai_loop_enabled=bool(spec.get("ai_loop_enabled", True)),
        portfolio_kind="paper",
        futu_sync_enabled=False,
        strategy_profile=spec["strategy_profile"],
        strategy_tags=spec["tags"],
        strategy_hypothesis=hypothesis,
        prompt_template=spec["prompt"],
        risk_overrides=spec["risk"],
    )
    portfolio = next(p for p in store.get("portfolios", []) if p.get("id") == portfolio_id)
    return {
        "id": portfolio_id,
        "name": portfolio["name"],
        "cash_by_currency": portfolio.get("cash_by_currency"),
        "apply_mode": portfolio.get("apply_mode"),
        "ai_loop_enabled": portfolio.get("ai_loop_enabled"),
        "strategy_profile": portfolio.get("strategy_profile"),
        "strategy_tags": portfolio.get("strategy_tags"),
        "risk_overrides": portfolio.get("risk_overrides"),
        "hypothesis": portfolio.get("strategy_hypothesis"),
    }


def main() -> None:
    rows = [_upsert_experiment(spec) for spec in EXPERIMENTS]
    print(json.dumps({"ok": True, "count": len(rows), "portfolios": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
