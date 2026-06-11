from __future__ import annotations

import json
import re
import socket
from typing import Any

from .config import AppConfig
from .futu_client import FutuPaperClient
from .news_signals import normalize_ticker, search_news_signals
from .portfolios import portfolio_context


COMMON_ALIASES = {
    "阿里巴巴": "HK.09988",
    "阿里": "HK.09988",
    "腾讯": "HK.00700",
    "英伟达": "US.NVDA",
    "苹果": "US.AAPL",
    "特斯拉": "US.TSLA",
}

POSITION_FIELDS = (
    "code",
    "stock_name",
    "qty",
    "can_sell_qty",
    "cost_price",
    "cost_price_valid",
    "nominal_price",
    "market_val",
    "pl_val",
    "pl_ratio",
    "today_pl_val",
    "today_pl_ratio",
)

QUOTE_FIELDS = (
    "code",
    "name",
    "last_price",
    "bid_price",
    "ask_price",
    "prev_close_price",
    "open_price",
    "high_price",
    "low_price",
    "volume",
    "turnover",
    "update_time",
)


def _usage_to_dict(usage: Any) -> dict[str, Any]:
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


def _clean_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        return []
    clean: list[dict[str, str]] = []
    for message in messages[-12:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        clean.append({"role": role, "content": content[:4000]})
    return clean


def _compact_row(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields if field in row and row.get(field) not in (None, "", [])}


def _extract_codes(text: str) -> list[str]:
    normalized: set[str] = set()
    upper_text = str(text or "").upper()
    for alias, code in COMMON_ALIASES.items():
        if alias in str(text or ""):
            normalized.add(code)

    ignored_plain_tokens = {"A", "AI", "BUY", "SELL", "HOLD", "USD", "HKD", "US", "HK", "CN", "IPO", "ETF"}
    patterns = (
        r"\b(?:US|HK|SH|SZ)\.?[A-Z0-9][A-Z0-9.\-]{0,9}\b",
        r"\b\d{1,5}\.HK\b",
        r"\b\d{6}\.(?:SH|SZ)\b",
        r"\b[A-Z]{2,5}\b",
    )
    for pattern in patterns:
        for raw in re.findall(pattern, upper_text):
            if raw in ignored_plain_tokens:
                continue
            if "." not in raw and not raw.startswith(("US", "HK", "SH", "SZ")) and len(raw) <= 2:
                continue
            code = normalize_ticker(raw)
            if code:
                normalized.add(code)
    return sorted(normalized)


def _load_trading_context(config: AppConfig, query: str, portfolio_id: str | None = None) -> dict[str, Any]:
    portfolio_payload = portfolio_context(portfolio_id)
    portfolio_positions = portfolio_payload.get("positions", [])
    portfolio_codes = [str(position.get("code", "")).upper() for position in portfolio_positions if position.get("code")]
    requested_codes = sorted(set(_extract_codes(query)) | set(portfolio_codes))
    try:
        with socket.create_connection((config.opend_host, config.opend_port), timeout=1.2):
            pass
    except OSError as exc:
        return {
            "requested_codes": requested_codes,
            "active_portfolio": portfolio_payload.get("portfolio", {}),
            "portfolio_positions": portfolio_positions,
            "futu_positions": [],
            "quotes": [],
            "price_rule": "当前价只能来自 Futu OpenD quotes；OpenD 不可用时必须说当前价缺失。",
            "errors": [f"opend:{config.opend_host}:{config.opend_port}:{exc}"],
        }

    client = FutuPaperClient(config)
    futu_positions: list[dict[str, Any]] = []
    errors: list[str] = []
    markets = sorted((config.gemini.observe_markets or config.risk.allowed_markets) & {"US", "HK", "CN"})
    if not markets:
        markets = ["US", "HK"]

    for market in markets:
        try:
            payload = client.positions(market)
        except Exception as exc:
            errors.append(f"positions:{market}:{exc}")
            continue
        if not payload.get("ok"):
            errors.append(f"positions:{market}:{payload.get('data') or payload.get('error') or 'failed'}")
            continue
        for row in payload.get("data") or []:
            if isinstance(row, dict):
                compact = _compact_row(row, POSITION_FIELDS)
                compact["market"] = market
                futu_positions.append(compact)

    futu_position_codes = [str(row.get("code", "")).upper() for row in futu_positions if row.get("code")]
    quote_codes = sorted(set(requested_codes) | set(futu_position_codes))[:24]
    quotes: list[dict[str, Any]] = []
    if quote_codes:
        try:
            payload = client.snapshot(quote_codes)
        except Exception as exc:
            errors.append(f"quotes:{exc}")
        else:
            if payload.get("ok"):
                quotes = [
                    _compact_row(row, QUOTE_FIELDS)
                    for row in payload.get("data") or []
                    if isinstance(row, dict)
                ]
            else:
                errors.append(f"quotes:{payload.get('data') or payload.get('error') or 'failed'}")

    return {
        "requested_codes": requested_codes,
        "active_portfolio": portfolio_payload.get("portfolio", {}),
        "portfolio_positions": portfolio_positions[:24],
        "futu_positions": futu_positions[:24],
        "quotes": quotes[:24],
        "price_rule": "当前价唯一可信来源是 Futu OpenD quotes 中的 last_price/bid_price/ask_price/update_time；联网检索价格只能当背景，不能当当前价。",
        "errors": errors[-6:],
    }


def _build_prompt(
    *,
    topic: str,
    messages: list[dict[str, str]],
    news_payload: dict[str, Any],
    trading_context: dict[str, Any],
    use_web: bool,
) -> str:
    return (
        "你是一个模拟盘交易教练，正在和一个交易新手讨论股票、行业或宏观主题。\n"
        "你的目标是帮助用户形成交易假设、理解买入/卖出/观望理由，而不是提供真实投资建议。\n"
        "你可以给出模拟盘倾向，但必须区分事实、推断和不确定性。\n"
        "最重要：用户最新一条消息优先级最高。你必须先理解用户到底在问什么，再逐项回答，不能只回答其中一个局部。\n\n"
        "回答规则：\n"
        "- 用中文，语气直接、清楚、适合新手。\n"
        "- 只能把 BUY / SELL / HOLD 当作模拟盘讨论倾向，不要暗示真实资金必然操作。\n"
        "- 如果用户给了持仓、成本价、买入价、亏损、盈利、时间周期或风险偏好，必须在回答中逐项使用这些信息。\n"
        "- 如果本地模拟盘里有持仓，必须优先使用 active_portfolio 和 portfolio_positions 作为用户真实持仓上下文。\n"
        "- 当前价格只能来自本地持仓/行情上下文里的 quotes.last_price、quotes.bid_price、quotes.ask_price 和 quotes.update_time。\n"
        "- 联网检索、新闻、网页摘要里的价格只能当背景，不能当作当前价；如果 quotes 没有对应标的，就明确说当前价缺失，不要编造或用网页价格替代。\n"
        "- 如果能得到当前价和成本价，必须计算大概浮动盈亏百分比；公式写清楚，结果可以取近似值。\n"
        "- 证据不足时优先 HOLD，并告诉用户还缺什么信息。\n"
        "- 不要编造财报、价格、新闻或公司事件；没有信息就明确说没有。\n"
        "- 如果使用了本地新闻库，必须点名引用具体新闻标题。\n"
        "- 如果用户问的是行业，先讲行业逻辑，再落到可观察标的。\n"
        "- 卖出建议要说明是否只适用于已有持仓；不要鼓励裸卖空。\n"
        "- 如果用户消息里有多个问题、多个条件或多个标的，请用编号逐项回应。\n"
        "- 不要用表格，避免前端渲染难看。\n"
        "- 最后给出一个小白能执行的观察清单。\n\n"
        "请用下面的 Markdown 结构回答：\n"
        "### 我理解的问题\n"
        "用 1-3 条复述用户真实想问什么，必须包含用户给出的成本、持仓或行业约束。\n"
        "### 结论\n"
        "一句话说明 BUY / SELL / HOLD 倾向、置信度，以及这只是模拟盘讨论。\n"
        "### 持仓/价格测算\n"
        "如果有持仓或成本，说明成本、当前价、浮盈浮亏；没有数据就说缺什么。\n"
        "### 支持继续持有或买入的理由\n"
        "列出支持因素。\n"
        "### 卖出/减仓/回避理由\n"
        "列出反向因素和风险。\n"
        "### 关键新闻\n"
        "引用本地新闻库或联网检索中真正相关的材料；无相关材料就说明。\n"
        "### 模拟盘动作\n"
        "给出一个保守的模拟盘动作建议，允许是继续观察。\n\n"
        "### 我还需要你补充什么\n"
        "列出为了下次判断更准确需要用户补充的 1-3 个信息。\n\n"
        f"讨论对象: {topic or '未指定'}\n"
        f"联网检索开关: {'已开启' if use_web else '未开启'}\n"
        f"本地持仓/行情上下文: {json.dumps(trading_context, ensure_ascii=False, default=str)[:16000]}\n"
        f"本地新闻库检索结果: {json.dumps(news_payload, ensure_ascii=False, default=str)[:24000]}\n"
        f"对话历史: {json.dumps(messages, ensure_ascii=False, default=str)[:16000]}\n"
    )


def run_ai_chat(
    config: AppConfig,
    *,
    topic: str,
    messages: Any,
    use_news: bool = True,
    use_web: bool = False,
    portfolio_id: str | None = None,
) -> dict[str, Any]:
    clean_messages = _clean_messages(messages)
    topic = str(topic or "").strip()[:200]
    latest_user = next((message["content"] for message in reversed(clean_messages) if message["role"] == "user"), "")
    query = " ".join(item for item in [topic, latest_user] if item).strip()

    if not config.gemini.api_key:
        return {
            "ok": False,
            "error": "GEMINI_API_KEY is missing.",
            "reply": "Gemini key 没有配置，所以现在还不能对话。",
            "news_signals": [],
            "news_notes": [],
        }
    if not query:
        return {
            "ok": False,
            "error": "topic or message is required.",
            "reply": "先输入一个股票、行业或问题，我再帮你一起拆。",
            "news_signals": [],
            "news_notes": [],
        }

    news_payload: dict[str, Any] = {"ok": True, "enabled": False, "signals": [], "notes": []}
    if use_news:
        news_payload = search_news_signals(config.news, query, limit=8)
    trading_context = _load_trading_context(config, query, portfolio_id)

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        return {
            "ok": False,
            "error": "google-genai is not installed. Run: pip install -r requirements.txt",
            "reply": str(exc),
            "news_signals": news_payload.get("signals", []),
            "news_notes": news_payload.get("notes", []),
        }

    client = genai.Client(api_key=config.gemini.api_key)
    max_output_tokens = max(1024, min(int(config.gemini.chat_max_output_tokens or 8000), 20000))
    prompt = _build_prompt(
        topic=topic,
        messages=clean_messages,
        news_payload=news_payload,
        trading_context=trading_context,
        use_web=use_web,
    )
    web_error = ""
    tools = None
    if use_web:
        try:
            tools = [types.Tool(googleSearch=types.GoogleSearch())]
        except Exception as exc:
            web_error = f"Google Search tool unavailable: {exc}"
            tools = None

    try:
        response = client.models.generate_content(
            model=config.gemini.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.25,
                max_output_tokens=max_output_tokens,
                tools=tools,
            ),
        )
    except Exception as exc:
        if not tools:
            return {
                "ok": False,
                "error": str(exc),
                "reply": f"Gemini 调用失败：{exc}",
                "news_signals": news_payload.get("signals", []),
                "news_notes": news_payload.get("notes", []),
                "web_error": web_error,
            }
        web_error = str(exc)
        response = client.models.generate_content(
            model=config.gemini.model,
            contents=_build_prompt(
                topic=topic,
                messages=clean_messages,
                news_payload=news_payload,
                trading_context=trading_context,
                use_web=False,
            ),
            config=types.GenerateContentConfig(
                temperature=0.25,
                max_output_tokens=max_output_tokens,
            ),
        )

    return {
        "ok": True,
        "topic": topic,
        "reply": getattr(response, "text", "") or "",
        "use_news": bool(use_news),
        "use_web": bool(use_web and not web_error),
        "web_error": web_error,
        "trading_context": trading_context,
        "news_signals": news_payload.get("signals", []),
        "news_notes": news_payload.get("notes", []),
        "max_output_tokens": max_output_tokens,
        "gemini_usage": _usage_to_dict(getattr(response, "usage_metadata", None)),
    }
