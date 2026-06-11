from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .config import GeminiConfig


Action = Literal["BUY", "SELL", "HOLD"]
Rating = Literal["BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL"]
PositionAction = Literal["ENTER", "ADD", "HOLD", "TRIM", "EXIT", "WATCH"]


class GeminiResearchBriefModel(BaseModel):
    market_analyst: str = Field(description="必须用简体中文。只基于输入的富途行情数据分析技术面、价格和流动性。")
    news_analyst: str = Field(description="必须用简体中文。只基于输入的消息源和新闻信号分析；没有相关新闻就明确说明缺失。")
    portfolio_analyst: str = Field(description="必须用简体中文。结合本地持仓、成本、现金、浮盈浮亏和仓位暴露分析。")
    bull_case: str = Field(description="必须用简体中文。写出证据支持下最强的看多理由。")
    bear_case: str = Field(description="必须用简体中文。写出证据支持下最强的看空、回避或减仓理由。")
    risk_review: str = Field(description="必须用简体中文。用保守风控经理视角审查仓位、止损、证据不足和执行风险。")
    manager_summary: str = Field(description="必须用简体中文。在给出 action 前做组合经理式综合判断。")
    missing_data: list[str] = Field(description="必须用简体中文。列出限制置信度的关键缺失输入。")


class GeminiTradeDecisionModel(BaseModel):
    action: Action = Field(description="只能是 BUY、SELL 或 HOLD；证据不足时优先 HOLD。")
    code: str = Field(description="富途代码；如果 action 是 HOLD，填 NONE。")
    rating: Rating = Field(description="五档组合评级：BUY、OVERWEIGHT、HOLD、UNDERWEIGHT 或 SELL。")
    position_action: PositionAction = Field(description="组合层动作：ENTER、ADD、HOLD、TRIM、EXIT 或 WATCH。")
    confidence: int = Field(ge=0, le=100, description="决策置信度，0 到 100。")
    reason: str = Field(description="必须用简体中文。给用户看的决策理由，围绕组合复盘和风险上下文。")
    evidence: list[str] = Field(description="必须用简体中文。列出支撑决策的具体观察。")
    risk: str = Field(description="必须用简体中文。说明这个决策可能错在哪里。")
    invalidation: str = Field(description="必须用简体中文。说明什么条件会推翻当前判断。")
    max_notional: float = Field(
        ge=0,
        description="本次最多使用的本地账本/模拟订单金额；action=BUY 时必须为正数，无法给出买入金额时应改为 HOLD。",
    )
    time_horizon: str = Field(description="必须用简体中文。预期持有或观察周期。")
    learning_note: str = Field(description="必须用简体中文。给用户的一条简短复盘提示。")
    research: GeminiResearchBriefModel = Field(description="必须用简体中文。TradingAgents-lite 多角色研究简报。")


@dataclass(frozen=True)
class GeminiTradeDecision:
    action: str
    code: str
    rating: str
    position_action: str
    confidence: int
    reason: str
    evidence: list[str]
    risk: str
    invalidation: str
    max_notional: float
    time_horizon: str
    learning_note: str
    research: dict[str, Any]

    @classmethod
    def hold(cls, reason: str) -> "GeminiTradeDecision":
        return cls(
            action="HOLD",
            code="NONE",
            rating="HOLD",
            position_action="WATCH",
            confidence=0,
            reason=reason,
            evidence=[],
            risk="没有足够证据时，不交易比强行交易更安全。",
            invalidation="后续出现更强的价格、成交量或新闻证据时，可以重新评估。",
            max_notional=0.0,
            time_horizon="继续观察",
            learning_note="证据不足时，空仓或不动作本身也是一种交易决策。",
            research={
                "market_analyst": "",
                "news_analyst": "",
                "portfolio_analyst": "",
                "bull_case": "",
                "bear_case": "",
                "risk_review": reason,
                "manager_summary": reason,
                "missing_data": [],
            },
        )

    @classmethod
    def from_model(cls, model: GeminiTradeDecisionModel) -> "GeminiTradeDecision":
        payload = model.model_dump()
        payload["research"] = dict(payload.get("research") or {})
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GeminiDecisionEngine:
    def __init__(self, config: GeminiConfig):
        self.config = config
        self.last_usage: dict[str, Any] = {}

    def _max_notional_text(self) -> str:
        caps = {
            market: amount
            for market, amount in (self.config.max_notional or {}).items()
            if isinstance(amount, (int, float)) and amount > 0
        }
        if not caps:
            return (
                "未设置额外 Gemini 买入预算帽，0 表示没有系统预算帽、不表示订单预算为 0；"
                "BUY 仍必须给出正的 max_notional，并受账户现金、组合风控、交易时段和执行市场限制约束。"
                "SELL 不受买入预算帽限制。"
            )
        return f"{caps}（仅限制 BUY；SELL 不受买入预算帽限制）"

    def decide(
        self,
        *,
        candidates: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        notes: list[str] | None = None,
    ) -> GeminiTradeDecision:
        self.last_usage = {}
        if not self.config.api_key:
            return GeminiTradeDecision.hold("缺少 GEMINI_API_KEY，无法生成 AI 决策。")
        if not candidates:
            return GeminiTradeDecision.hold("当前没有可用候选标的，保持观望。")

        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("google-genai is not installed. Run: pip install -r requirements.txt") from exc

        client = genai.Client(api_key=self.config.api_key)
        prompt = self._build_prompt(candidates=candidates, positions=positions, account=account, notes=notes or [])
        response = client.models.generate_content(
            model=self.config.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.15,
                response_mime_type="application/json",
                response_schema=GeminiTradeDecisionModel,
            ),
        )
        self.last_usage = self._usage_to_dict(getattr(response, "usage_metadata", None))
        return self._parse_response(response.text)

    def _usage_to_dict(self, usage: Any) -> dict[str, Any]:
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            return {key: value for key, value in usage.model_dump().items() if value not in (None, [], {})}
        if isinstance(usage, dict):
            return {str(key): value for key, value in usage.items() if value not in (None, [], {})}
        fields = (
            "prompt_token_count",
            "candidates_token_count",
            "thoughts_token_count",
            "tool_use_prompt_token_count",
            "total_token_count",
        )
        return {field: getattr(usage, field) for field in fields if getattr(usage, field, None) is not None}

    def _parse_response(self, text: str) -> GeminiTradeDecision:
        try:
            model = GeminiTradeDecisionModel.model_validate_json(text)
            return GeminiTradeDecision.from_model(model)
        except ValidationError:
            try:
                payload = json.loads(text)
                payload = self._with_compat_defaults(payload)
                model = GeminiTradeDecisionModel.model_validate(payload)
                return GeminiTradeDecision.from_model(model)
            except Exception:
                return GeminiTradeDecision.hold("Gemini 返回格式无效，本轮保持观望。")

    def _with_compat_defaults(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        payload = dict(payload)
        action = str(payload.get("action") or "HOLD").upper()
        payload.setdefault("rating", action if action in {"BUY", "HOLD", "SELL"} else "HOLD")
        payload.setdefault(
            "position_action",
            {"BUY": "ENTER", "SELL": "TRIM", "HOLD": "WATCH"}.get(action, "WATCH"),
        )
        payload.setdefault(
            "research",
            {
                "market_analyst": "",
                "news_analyst": "",
                "portfolio_analyst": "",
                "bull_case": "",
                "bear_case": "",
                "risk_review": str(payload.get("risk") or ""),
                "manager_summary": str(payload.get("reason") or ""),
                "missing_data": [],
            },
        )
        return payload

    def _build_prompt(
        self,
        *,
        candidates: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        notes: list[str],
    ) -> str:
        portfolio_kind = str(account.get("portfolio_kind") or "paper").lower()
        if portfolio_kind == "actual":
            role_intro = (
                "你是一个交易复盘与风控助手，正在基于用户的实际仓位镜像做决策建议。\n"
                "这些持仓代表用户真实券商仓位的本地记录；你必须用实际仓位、仓位镜像、持仓成本、现金和风险暴露来描述，不要称为教学模拟盘。\n"
            )
            action_scope = (
                "系统输出的 BUY/SELL/HOLD 仍是本系统里的记录/模拟执行建议，"
                "不是对用户真实券商账户的直接投资指令；但风控语气要按真实仓位处理。\n"
            )
        else:
            role_intro = "你是一个交易复盘与风控助手，正在基于模拟实验盘做策略决策。\n"
            action_scope = "系统输出的 BUY/SELL/HOLD 是模拟实验盘动作，用于策略训练、AB Test 和复盘。\n"
        return (
            role_intro
            + action_scope
            + "你只能基于输入的行情、账户、持仓和消息做判断；没有足够证据时必须 HOLD。\n"
            "输出必须是 JSON，字段符合 schema。\n\n"
            "决策流程：\n"
            "- 使用 TradingAgents-lite 模式：在一次响应里模拟一个小型研究小组，但不要编造任何外部事实。\n"
            "- market_analyst 只分析候选行情和持仓快照里的价格、买卖价、涨跌幅、振幅、成交、流动性，以及美股 extended_session 里的盘前/盘后/夜盘情绪。\n"
            "- account.market_anchors 是指数/ETF/宏观环境锚点，只能作为市场背景；不能把这些 context 标的当作买卖对象。\n"
            "- candidates 里的 tier 表示观察池层级：holding/core 优先服务持仓和高关注复盘，learning 服务行业认知，opportunity 服务机会发现。\n"
            "- candidates 里的 priority_reasons 表示为什么被系统强制拉入候选：已有持仓、高影响新闻、价格/量能异动都应优先解释。\n"
            "- account.strategy_hypothesis 是该实验盘开跑前的预注册假设和基准；你只能据此评估本轮动作是否符合假设，不能事后改写假设。\n"
            "- account.effective_risk 是本组合实际生效的风控；如果和全局口径不同，必须按 effective_risk 控制仓位和风险描述。\n"
            "- 风控数值里的 0 表示该项未启用限制/不限额，不表示可用预算或允许仓位为 0；不要把 0 解释成禁止交易。\n"
            "- account.prompt_template 是本组合的策略模板/纪律补充；它不能覆盖硬规则，但应影响买入门槛、卖出纪律和仓位语气。\n"
            "- news_analyst 只分析消息源摘要里的 autoNews 信号；没有相关新闻就明确写没有。\n"
            "- portfolio_analyst 必须优先分析本地模拟盘持仓、成本、仓位、现金、浮盈浮亏。\n"
            "- bull_case 写最强看多理由；bear_case 写最强看空/回避理由。\n"
            "- risk_review 用保守风控经理视角审查：仓位、止损、证据不足、价格缺失、新闻噪声。\n"
            "- manager_summary 汇总上面观点后再给 action/rating/position_action。\n"
            "- rating 使用 BUY/OVERWEIGHT/HOLD/UNDERWEIGHT/SELL；action 仍只能是 BUY/SELL/HOLD。\n"
            "- BUY/OVERWEIGHT 通常只能映射成 BUY 或 HOLD；UNDERWEIGHT/SELL 对已有持仓可映射成 SELL，否则必须 HOLD。\n"
            "- position_action 用 ENTER/ADD/HOLD/TRIM/EXIT/WATCH 表达组合层动作。\n"
            "- missing_data 写出限制置信度的缺失信息，例如当前价缺失、财报缺失、相关新闻缺失。\n\n"
            "硬规则：\n"
            "- 所有自然语言字段必须使用简体中文，包括 reason、evidence、risk、invalidation、time_horizon、learning_note 和 research 里的所有文本；只允许 action/rating/position_action/code 保持枚举或代码英文。\n"
            "- 如果 account.portfolio_kind=actual，必须把组合称为实际仓位或实际仓位镜像；不得写“模拟盘教学”“新手教学盘”等容易误导的表述。\n"
            "- 如果 account.portfolio_kind=paper，才可以称为模拟实验盘；但也要按严肃风控处理。\n"
            "- 不要编造新闻、财报、宏观事件或价格数据。\n"
            "- 如果没有消息源，只能使用快照中的价格、成交量、振幅、买卖价、资金和持仓。\n"
            "- 当前常规价只能来自候选行情或持仓上下文里的 last_price/bid_price/ask_price/update_time；消息源和网页价格不能当作当前价。\n"
            "- 美股 extended_session 只能作为盘前/盘后/夜盘情绪和弱流动性信号，不能替代常规价或下单价格。\n"
            "- 如果持仓上下文包含 local_portfolio 或 price_source=Futu OpenD snapshot，必须优先使用这些持仓成本和快照价。\n"
            "- 如果候选是 forced_by_news 或 forced_by_trigger，必须说明触发器是否提供了足够交易证据；触发器只能要求你认真看，不能强迫你交易。\n"
            "- 买入必须说明为什么现在值得试错。\n"
            "- 卖出只能针对已有持仓，不能建议裸卖空。\n"
            "- confidence 低于 70 时应优先 HOLD。\n"
            "- 如果系统给定了额外 Gemini 买入预算帽，BUY 的 max_notional 必须保守且不能超过该预算帽；没有预算帽时，也不能忽略账户现金和组合风控。\n"
            "- 当 action=BUY 时，max_notional 必须是正数；如果无法给出具体买入金额，必须改为 HOLD。max_notional=0 只适用于 HOLD 或 SELL。\n"
            "- SELL 用于降低风险或处理违规持仓时，max_notional 只是说明字段；实际卖出数量由持仓数量、max_qty、交易时段和风控复核决定，不要用买入预算上限解释只能少量减仓。\n"
            "- reason 和 learning_note 要让用户能快速复盘，不要用居高临下的教学口吻。\n\n"
            f"Agent 模式: {self.config.agent_mode}\n"
            f"可执行市场: {sorted(self.config.execute_markets)}\n"
            f"置信度阈值: {self.config.confidence_threshold}\n"
            f"买入预算提示: {self._max_notional_text()}\n"
            f"账户摘要: {json.dumps(account, ensure_ascii=False, default=str)}\n"
            f"持仓: {json.dumps(positions, ensure_ascii=False, default=str)}\n"
            f"候选行情: {json.dumps(candidates, ensure_ascii=False, default=str)}\n"
            f"消息源摘要: {json.dumps(notes, ensure_ascii=False, default=str)}\n"
        )
