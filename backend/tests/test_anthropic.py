"""Anthropic 翻译器与预检测试（mock HTTP）。"""

import httpx
import pytest

from app.services import babeldoc_runner
from app.services.anthropic_translator import AnthropicTranslator
from app.services.babeldoc_runner import PreflightError


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def test_anthropic_do_translate_parses_text_blocks(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResp(payload={
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "敏捷的棕色狐狸"},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    t = AnthropicTranslator("en", "zh", model="qwen3.8-max",
                            base_url="https://gw/apps/anthropic",
                            api_key="sk-test")
    out = t.do_translate("Hello world")
    assert out == "敏捷的棕色狐狸"
    assert captured["url"] == "https://gw/apps/anthropic/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["json"]["temperature"] == 0
    assert captured["json"]["system"].startswith("You are a professional")
    assert "Hello world" in captured["json"]["messages"][0]["content"]
    assert t.prompt_token_count.value == 10 if hasattr(t.prompt_token_count, "value") else True


def test_anthropic_llm_translate_no_system(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return FakeResp(payload={
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    t = AnthropicTranslator("en", "zh", model="m", base_url="https://gw",
                            api_key="k")
    assert t.do_llm_translate("line1") == "x"
    assert "system" not in captured["json"]
    assert captured["json"]["max_tokens"] == 2048


def test_preflight_pass_and_fail(monkeypatch):
    class Good:
        def translate(self, text):
            return "好的"

    class Bad:
        def translate(self, text):
            raise RuntimeError("HTTP 404")

    class Empty:
        def translate(self, text):
            return "  "

    babeldoc_runner.preflight(Good())

    with pytest.raises(PreflightError, match="404"):
        babeldoc_runner.preflight(Bad())
    with pytest.raises(PreflightError, match="空结果"):
        babeldoc_runner.preflight(Empty())


def test_build_translator_protocol(monkeypatch):
    from app.models import Profile

    p_open = Profile(id=1, name="a", model="m", protocol="openai")
    p_anth = Profile(id=2, name="b", model="m", protocol="anthropic")
    p_legacy = Profile(id=3, name="c", model="m")  # 旧库无 protocol 列的场景

    from babeldoc.translator.translator import OpenAITranslator

    assert isinstance(babeldoc_runner.build_translator(p_open, "en", "zh"),
                      OpenAITranslator)
    assert isinstance(babeldoc_runner.build_translator(p_anth, "en", "zh"),
                      AnthropicTranslator)
    assert isinstance(babeldoc_runner.build_translator(p_legacy, "en", "zh"),
                      OpenAITranslator)
