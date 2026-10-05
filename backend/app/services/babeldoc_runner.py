"""BabelDOC 隔离层：整个后端只有本模块直接接触 BabelDOC。

对外暴露：
- ensure_init() / assets_status() / warmup_assets()
- get_layout_model()
- build_translation_config(...)
- translate_stream(config)：把 async_translate 的原始事件规范化为
    {"type": "progress", stage, stage_progress(0-100), stage_current, stage_total, overall_progress}
    {"type": "finish", result: TranslateResult}
  出错时抛出 BabelDOCError。
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import babeldoc.assets.assets as babel_assets
import babeldoc.format.pdf.high_level as high_level
from babeldoc.docvision.doclayout import DocLayoutModel
from babeldoc.format.pdf.high_level import async_translate
from babeldoc.format.pdf.translation_config import (
    TranslationConfig,
    WatermarkOutputMode,
)
from babeldoc.glossary import Glossary
from babeldoc.translator.translator import OpenAITranslator

from app.models import Profile

logger = logging.getLogger(__name__)


class BabelDOCError(RuntimeError):
    pass


@dataclass
class RunnerOptions:
    """任务级翻译选项（Profile 之外的部分）。"""

    input_file: Path
    output_dir: Path
    lang_in: str
    lang_out: str
    pages: str | None = None
    scan_mode: str = "off"  # off|workaround|auto
    glossaries: list[Glossary] | None = None
    auto_extract_glossary: bool = False


_layout_model: DocLayoutModel | None = None
_assets_ready: bool | None = None
_init_done = False


def ensure_init() -> None:
    """创建 BabelDOC 缓存目录等初始化（幂等）。"""
    global _init_done
    if not _init_done:
        high_level.init()
        _init_done = True


def get_layout_model() -> DocLayoutModel:
    """加载并缓存版面识别模型（需要先完成资产下载）。"""
    global _layout_model, _assets_ready
    if _layout_model is None:
        ensure_init()
        _layout_model = DocLayoutModel.load_onnx()
        _assets_ready = True
    return _layout_model


def assets_status() -> dict:
    """检查资产是否就绪（模型能否加载）。线程内调用，可能较慢。"""
    global _assets_ready
    if _assets_ready:
        return {"ready": True, "message": "ok"}
    try:
        get_layout_model()
        return {"ready": True, "message": "ok"}
    except Exception as e:  # noqa: BLE001
        logger.warning("assets not ready: %s", e)
        return {"ready": False, "message": str(e)}


def warmup_assets() -> dict:
    """下载字体与模型资产（阻塞，放线程里跑）。"""
    global _assets_ready
    ensure_init()
    babel_assets.warmup()
    try:
        get_layout_model()
    except Exception as e:  # noqa: BLE001
        return {"ready": False, "message": str(e)}
    _assets_ready = True
    return {"ready": True, "message": "ok"}


def build_translation_config(profile: Profile, opts: RunnerOptions) -> TranslationConfig:
    translator = OpenAITranslator(
        lang_in=opts.lang_in,
        lang_out=opts.lang_out,
        model=profile.model,
        base_url=profile.base_url or None,
        # 本地推理服务（Ollama/LM Studio 等）通常不校验 key，占位即可
        api_key=profile.api_key or "EMPTY",
    )
    model = get_layout_model()
    config = TranslationConfig(
        translator=translator,
        input_file=opts.input_file,
        output_dir=opts.output_dir,
        lang_in=opts.lang_in,
        lang_out=opts.lang_out,
        pages=opts.pages or None,
        doc_layout_model=model,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        qps=profile.qps,
        pool_max_workers=profile.pool_max_workers,
        no_mono=False,
        no_dual=False,
        ocr_workaround=(opts.scan_mode == "workaround"),
        skip_scanned_detection=(opts.scan_mode == "workaround"),
        auto_enable_ocr_workaround=(opts.scan_mode == "auto"),
        glossaries=opts.glossaries or None,
        auto_extract_glossary=opts.auto_extract_glossary,
        custom_system_prompt=profile.custom_system_prompt or None,
    )
    # CLI 同款调用：把字体映射器挂到模型上
    getattr(model, "init_font_mapper", lambda _c: None)(config)
    return config


async def translate_stream(config: TranslationConfig) -> AsyncIterator[dict]:
    """消费 async_translate 事件流并规范化；error 事件转异常。"""
    it = async_translate(config).__aiter__()
    try:
        while True:
            try:
                event = await it.__anext__()
            except StopAsyncIteration:
                break
            etype = event.get("type")
            if etype in ("progress_start", "progress_update", "progress_end"):
                yield {
                    "type": "progress",
                    "stage": event.get("stage"),
                    "stage_progress": float(event.get("stage_progress") or 0.0),
                    "stage_current": event.get("stage_current"),
                    "stage_total": event.get("stage_total"),
                    "overall_progress": float(event.get("overall_progress") or 0.0),
                }
            elif etype == "finish":
                yield {"type": "finish", "result": event["translate_result"]}
                return
            elif etype == "error":
                raise BabelDOCError(str(event.get("error") or "unknown error"))
    except asyncio.CancelledError:
        # 让 async_translate 内部的取消逻辑生效（cancel_event.set()）
        raise
