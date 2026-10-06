"""解析 PDF 并导出段落中间表示（paragraph_finder.json），不调用 LLM。

用法：uv run python ../scripts/dump_il.py <pdf>
输出：data/il_dump/paragraph_finder.json + 控制台摘要
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from babeldoc.docvision.doclayout import DocLayoutModel
from babeldoc.format.pdf.high_level import async_translate
from babeldoc.format.pdf.translation_config import (
    TranslationConfig,
    WatermarkOutputMode,
)


async def main():
    pdf = Path(sys.argv[1])
    out = Path("data/il_dump")
    out.mkdir(parents=True, exist_ok=True)
    cfg = TranslationConfig(
        translator=type("T", (), {})(),  # skip_translation=True 不会用到
        input_file=pdf,
        output_dir=out,
        working_dir=out,
        lang_in="en", lang_out="zh",
        doc_layout_model=DocLayoutModel.load_onnx(),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        skip_translation=True,
        debug=True,
    )
    async for ev in async_translate(cfg):
        if ev.get("type") in ("finish", "error"):
            print(ev.get("type"))

    data = json.loads((out / "paragraph_finder.json").read_text("utf-8"))
    pages = data["document"]["page"] if "document" in data else data["page"]
    page1 = pages[0] if isinstance(pages, list) else pages["0"]
    paras = page1["pdf_paragraph"]
    print(f"page1 paragraphs: {len(paras)}")
    for i, p in enumerate(paras):
        comps = p.get("pdf_paragraph_composition") or []
        texts = []
        for c in comps:
            if c.get("pdf_line"):
                chars = "".join(
                    ch.get("char_unicode", "")
                    for ch in c["pdf_line"].get("pdf_character") or []
                )
            elif c.get("pdf_character"):
                chars = c["pdf_character"].get("char_unicode", "")
            else:
                chars = "<atomic>"
            texts.append(chars)
        u = p.get("unicode", "")
        if "recent" in u or "technolog" in u or "improve" in u or len(u) < 20:
            box = p.get("box", {})
            print(f"[{i}] box=({round(box.get('x',0))},{round(box.get('y',0))},{round(box.get('x2',0))},{round(box.get('y2',0))}) comps={len(comps)}")
            print(f"    unicode={u[:90]!r}")
            for t in texts:
                print(f"      comp: {t[:70]!r}")


asyncio.run(main())
