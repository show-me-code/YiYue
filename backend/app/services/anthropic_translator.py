"""Anthropic 协议翻译器：复用 BabelDOC OpenAITranslator 的提示词/占位符/缓存，
仅把 HTTP 调用换成 Anthropic Messages API（POST {base}/v1/messages）。

适用于 Anthropic 原生端点以及兼容该协议的网关（x-api-key + anthropic-version 头）。
"""

import logging

import httpx
from tenacity import (before_sleep_log, retry, retry_if_exception,
                      stop_after_attempt, wait_exponential)

from babeldoc.translator.translator import OpenAITranslator

logger = logging.getLogger(__name__)


def _is_rate_limit(e: BaseException) -> bool:
    return (isinstance(e, httpx.HTTPStatusError)
            and e.response.status_code == 429)


class AnthropicTranslator(OpenAITranslator):
    name = "anthropic"

    def __init__(self, lang_in, lang_out, model, base_url=None, api_key=None,
                 send_temperature=True):
        super().__init__(lang_in, lang_out, model, base_url=base_url,
                         api_key=api_key or "EMPTY",
                         send_temperature=send_temperature)
        self.base = (base_url or "https://api.anthropic.com").rstrip("/")
        self.headers = {
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
        }
        self.add_cache_impact_parameters("protocol", "anthropic")
        self.client = None  # 不使用 OpenAI SDK 客户端

    def _complete(self, user_text: str, system_text: str | None,
                  max_tokens: int) -> str:
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_text}],
        }
        if system_text:
            body["system"] = system_text
        if self.send_temperature:
            body["temperature"] = 0
        r = httpx.post(self.base + "/v1/messages", headers=self.headers,
                       json=body, timeout=600)
        r.raise_for_status()
        data = r.json()
        self._update_token_count(data.get("usage") or {})
        content = data.get("content") or []
        return "".join(
            b.get("text", "") for b in content if b.get("type") == "text"
        ).strip()

    def _update_token_count(self, usage: dict) -> None:
        try:
            tin = int(usage.get("input_tokens") or 0)
            tout = int(usage.get("output_tokens") or 0)
            self.prompt_token_count.inc(tin)
            self.completion_token_count.inc(tout)
            self.token_count.inc(tin + tout)
        except Exception:  # noqa: BLE001
            logger.debug("token count update failed", exc_info=True)

    @retry(
        retry=retry_if_exception(_is_rate_limit),
        stop=stop_after_attempt(100),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def do_translate(self, text, rate_limit_params: dict = None) -> str:
        msgs = self.prompt(text)  # 与 OpenAI 翻译器完全相同的提示词结构
        return self._complete(msgs[1]["content"], msgs[0]["content"], 4096)

    @retry(
        retry=retry_if_exception(_is_rate_limit),
        stop=stop_after_attempt(100),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def do_llm_translate(self, text, rate_limit_params: dict = None) -> str:
        if text is None:
            return None
        # 与 OpenAI do_llm_translate 一致：仅用户消息，不带系统提示
        return self._complete(text, None, 2048)
