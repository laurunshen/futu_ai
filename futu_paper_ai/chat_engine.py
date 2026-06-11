from __future__ import annotations

import json
from typing import Any

from .config import AppConfig
from .news_signals import search_news_signals


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


def _build_prompt(
    *,
    topic: str,
    messages: list[dict[str, str]],
    news_payload: dict[str, Any],
    use_web: bool,
) -> str:
    return (
        "你是一个模拟盘交易教练，正在和一个交易新手讨论股票、行业或宏观主题。\n"
        "你的目标是帮助用户形成交易假设、理解买入/卖出/观望理由，而不是提供真实投资建议。\n"
        "你可以给出模拟盘倾向，但必须区分事实、推断和不确定性。\n\n"
        "回答规则：\n"
        "- 用中文，语气直接、清楚、适合新手。\n"
        "- 只能把 BUY / SELL / HOLD 当作模拟盘讨论倾向，不要暗示真实资金必然操作。\n"
        "- 证据不足时优先 HOLD，并告诉用户还缺什么信息。\n"
        "- 不要编造财报、价格、新闻或公司事件；没有信息就明确说没有。\n"
        "- 如果使用了本地新闻库，必须点名引用具体新闻标题。\n"
        "- 如果用户问的是行业，先讲行业逻辑，再落到可观察标的。\n"
        "- 卖出建议要说明是否只适用于已有持仓；不要鼓励裸卖空。\n"
        "- 最后给出一个小白能执行的观察清单。\n\n"
        "请用下面的 Markdown 结构回答：\n"
        "### 倾向\n"
        "一句话说明 BUY / SELL / HOLD 倾向和置信度。\n"
        "### 买入理由\n"
        "列出支持试错的因素。\n"
        "### 卖出/回避理由\n"
        "列出反向因素和风险。\n"
        "### 关键新闻\n"
        "引用本地新闻库或联网检索中真正相关的材料；无相关材料就说明。\n"
        "### 观察清单\n"
        "给出后续需要观察的 3-5 个信号。\n"
        "### 模拟盘动作\n"
        "给出一个保守的模拟盘动作建议，允许是继续观察。\n\n"
        f"讨论对象: {topic or '未指定'}\n"
        f"联网检索开关: {'已开启' if use_web else '未开启'}\n"
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
    prompt = _build_prompt(topic=topic, messages=clean_messages, news_payload=news_payload, use_web=use_web)
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
                max_output_tokens=1800,
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
            contents=_build_prompt(topic=topic, messages=clean_messages, news_payload=news_payload, use_web=False),
            config=types.GenerateContentConfig(
                temperature=0.25,
                max_output_tokens=1800,
            ),
        )

    return {
        "ok": True,
        "topic": topic,
        "reply": getattr(response, "text", "") or "",
        "use_news": bool(use_news),
        "use_web": bool(use_web and not web_error),
        "web_error": web_error,
        "news_signals": news_payload.get("signals", []),
        "news_notes": news_payload.get("notes", []),
        "gemini_usage": _usage_to_dict(getattr(response, "usage_metadata", None)),
    }
