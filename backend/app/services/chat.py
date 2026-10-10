"""AI 阅读助手：基于论文全文的问答，复用任务所属配置档（OpenAI / Anthropic 协议）。"""

import httpx

from app.models import Profile

_TIMEOUT = 300  # 长回答生成可能较慢
_MAX_TOKENS = 4096

_SYSTEM_TEMPLATE = (
    "你是论文阅读助手。用户正在阅读一篇学术论文，下面是从 PDF 按段落抽取的论文正文"
    "（可能不完整或含少量噪声）。请基于论文内容回答用户问题，默认用中文；"
    "论文中没有提及的内容要明确说明，不要编造。\n\n"
    "【论文正文】\n{context}"
)


class ChatError(RuntimeError):
    """LLM 调用失败（网络/鉴权/限流等），消息可直接展示给用户。"""


def _post(url: str, headers: dict, body: dict) -> httpx.Response:
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise ChatError(f"连接翻译服务失败：{e}") from e
    if resp.status_code != 200:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:  # noqa: BLE001
            pass
        raise ChatError(f"HTTP {resp.status_code}：{detail}")
    return resp


def _openai_reply(profile: Profile, system: str, messages: list[dict]) -> str:
    base = (profile.base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {profile.api_key}"} \
        if profile.api_key else {}
    body = {
        "model": profile.model,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    data = _post(base + "/chat/completions", headers, body).json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        raise ChatError("响应格式无法解析（非 OpenAI 兼容结构）") from e


def _anthropic_reply(profile: Profile, system: str, messages: list[dict]) -> str:
    base = (profile.base_url or "https://api.anthropic.com").rstrip("/")
    headers = {
        "x-api-key": profile.api_key or "",
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": profile.model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        # Anthropic 只允许 user/assistant 交替，且首条必须是 user
        "messages": [m for m in messages if m["role"] in ("user", "assistant")],
    }
    data = _post(base + "/v1/messages", headers, body).json()
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content
                   if b.get("type") == "text").strip()
    if not text:
        raise ChatError("响应格式无法解析（非 Anthropic 兼容结构）")
    return text


def chat_reply(profile: Profile, context: str, messages: list[dict]) -> str:
    """以论文正文为上下文做一次问答，返回助手回复文本。"""
    system = _SYSTEM_TEMPLATE.format(context=context or "（正文暂不可用）")
    if profile.custom_system_prompt:
        system += "\n\n" + profile.custom_system_prompt
    protocol = getattr(profile, "protocol", "openai") or "openai"
    if protocol == "anthropic":
        return _anthropic_reply(profile, system, messages)
    return _openai_reply(profile, system, messages)
