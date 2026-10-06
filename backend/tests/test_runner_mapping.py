"""babeldoc_runner 配置映射与事件规范化测试（不真正翻译）。"""

import pytest

from app.models import Profile
from app.services import babeldoc_runner
from app.services.babeldoc_runner import RunnerOptions


@pytest.fixture()
def stub_model(monkeypatch):
    class Stub:
        pass

    monkeypatch.setattr(babeldoc_runner, "get_layout_model", lambda: Stub())
    return Stub()


def test_build_translation_config_mapping(stub_model, tmp_path):
    profile = Profile(
        id=1, name="p", base_url="http://llm/v1", api_key="k", model="m",
        qps=3, pool_max_workers=5, custom_system_prompt="Be terse.")
    opts = RunnerOptions(
        input_file=tmp_path / "in.pdf", output_dir=tmp_path / "out",
        lang_in="en", lang_out="zh", pages="1-3", scan_mode="workaround",
    )
    cfg = babeldoc_runner.build_translation_config(profile, opts)
    assert cfg.lang_in == "en" and cfg.lang_out == "zh"
    assert cfg.pages == "1-3"
    assert cfg.qps == 3 and cfg.pool_max_workers == 5
    assert cfg.ocr_workaround is True
    assert cfg.skip_scanned_detection is True
    assert cfg.auto_enable_ocr_workaround is False
    assert cfg.custom_system_prompt == "Be terse."
    assert cfg.watermark_output_mode.value == "no_watermark"
    assert cfg.output_dir == tmp_path / "out"
    # 排版选项保持引擎默认：段落修复由 babeldoc 补丁（scripts/apply_babeldoc_patches.py）承担
    assert cfg.split_short_lines is False
    assert cfg.min_text_length == 5


def test_scan_mode_auto(stub_model, tmp_path):
    profile = Profile(id=1, name="p", model="m")
    opts = RunnerOptions(
        input_file=tmp_path / "in.pdf", output_dir=tmp_path / "out",
        lang_in="en", lang_out="zh", scan_mode="auto")
    cfg = babeldoc_runner.build_translation_config(profile, opts)
    assert cfg.auto_enable_ocr_workaround is True
    assert cfg.ocr_workaround is False


async def test_translate_stream_normalization(monkeypatch):
    class FakeGen:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i == 0:
                self._i = 1
                return {"type": "progress_update", "stage": "Translate",
                        "stage_progress": 50, "stage_current": 1,
                        "stage_total": 2, "overall_progress": 25.0}
            if self._i == 1:
                self._i = 2
                return {"type": "finish", "translate_result": "RESULT"}
            raise StopAsyncIteration

        _i = 0

    monkeypatch.setattr(babeldoc_runner, "async_translate",
                        lambda cfg: FakeGen())
    from app.services.babeldoc_runner import translate_stream

    events = [ev async for ev in translate_stream(None)]
    assert events[0]["type"] == "progress"
    assert events[0]["stage_progress"] == 50
    assert events[-1] == {"type": "finish", "result": "RESULT"}


async def test_translate_stream_error_event(monkeypatch):
    class FakeGen:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._done:
                self._done = True
                return {"type": "error", "error": "boom"}
            raise StopAsyncIteration

        _done = False

    from app.services.babeldoc_runner import BabelDOCError, translate_stream

    monkeypatch.setattr(babeldoc_runner, "async_translate",
                        lambda cfg: FakeGen())
    with pytest.raises(BabelDOCError, match="boom"):
        async for _ in translate_stream(None):
            pass
