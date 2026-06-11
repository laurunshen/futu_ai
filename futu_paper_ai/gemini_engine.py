from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .config import GeminiConfig


Action = Literal["BUY", "SELL", "HOLD"]


class GeminiTradeDecisionModel(BaseModel):
    action: Action = Field(description="BUY, SELL, or HOLD. Prefer HOLD when evidence is weak.")
    code: str = Field(description="Futu code to trade, or NONE when action is HOLD.")
    confidence: int = Field(ge=0, le=100, description="Decision confidence from 0 to 100.")
    reason: str = Field(description="Plain-language reason for a beginner.")
    evidence: list[str] = Field(description="Concrete observations used for the decision.")
    risk: str = Field(description="What can go wrong with this decision.")
    invalidation: str = Field(description="What would prove the idea wrong.")
    max_notional: float = Field(ge=0, description="Maximum simulated order value to use.")
    time_horizon: str = Field(description="Expected holding horizon.")
    learning_note: str = Field(description="One educational takeaway for the user.")


@dataclass(frozen=True)
class GeminiTradeDecision:
    action: str
    code: str
    confidence: int
    reason: str
    evidence: list[str]
    risk: str
    invalidation: str
    max_notional: float
    time_horizon: str
    learning_note: str

    @classmethod
    def hold(cls, reason: str) -> "GeminiTradeDecision":
        return cls(
            action="HOLD",
            code="NONE",
            confidence=0,
            reason=reason,
            evidence=[],
            risk="No trade is safer than forcing a weak setup.",
            invalidation="New evidence with stronger price/volume/news support appears.",
            max_notional=0.0,
            time_horizon="observe",
            learning_note="When evidence is thin, doing nothing is also a trading decision.",
        )

    @classmethod
    def from_model(cls, model: GeminiTradeDecisionModel) -> "GeminiTradeDecision":
        return cls(**model.model_dump())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GeminiDecisionEngine:
    def __init__(self, config: GeminiConfig):
        self.config = config
        self.last_usage: dict[str, Any] = {}

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
            return GeminiTradeDecision.hold("GEMINI_API_KEY is missing.")
        if not candidates:
            return GeminiTradeDecision.hold("No valid candidates were available.")

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
                model = GeminiTradeDecisionModel.model_validate(payload)
                return GeminiTradeDecision.from_model(model)
            except Exception:
                return GeminiTradeDecision.hold("Gemini returned an invalid decision format.")

    def _build_prompt(
        self,
        *,
        candidates: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        notes: list[str],
    ) -> str:
        return (
            "你是一个模拟盘交易教练，目标是帮助新手学习，而不是追求激进收益。\n"
            "你只能基于输入的行情、账户、持仓和消息做判断；没有足够证据时必须 HOLD。\n"
            "输出必须是 JSON，字段符合 schema。\n\n"
            "硬规则：\n"
            "- 这是模拟盘，但也要当作真实训练处理。\n"
            "- 不要编造新闻、财报、宏观事件或价格数据。\n"
            "- 如果没有消息源，只能使用快照中的价格、成交量、振幅、买卖价、资金和持仓。\n"
            "- 当前价只能来自候选行情或持仓上下文里的 last_price/bid_price/ask_price/update_time；消息源和网页价格不能当作当前价。\n"
            "- 如果持仓上下文包含 local_portfolio 或 price_source=Futu OpenD snapshot，必须优先使用这些持仓成本和快照价。\n"
            "- 买入必须说明为什么现在值得试错。\n"
            "- 卖出只能针对已有持仓，不能建议裸卖空。\n"
            "- confidence 低于 70 时应优先 HOLD。\n"
            "- max_notional 必须保守，不能超过系统给定上限。\n"
            "- reason 和 learning_note 要让小白能看懂。\n\n"
            f"可执行市场: {sorted(self.config.execute_markets)}\n"
            f"置信度阈值: {self.config.confidence_threshold}\n"
            f"单笔上限: {self.config.max_notional}\n"
            f"账户摘要: {json.dumps(account, ensure_ascii=False, default=str)}\n"
            f"持仓: {json.dumps(positions, ensure_ascii=False, default=str)}\n"
            f"候选行情: {json.dumps(candidates, ensure_ascii=False, default=str)}\n"
            f"消息源摘要: {json.dumps(notes, ensure_ascii=False, default=str)}\n"
        )
