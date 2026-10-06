"""排版选项对照实验：只用第 1 页，比较不同 BabelDOC 选项的输出效果。

用法：cd backend && uv run python ../scripts/layout_experiments.py <pdf> [page_expr]
输出：data/exp/<名称>.png（渲染后的译文第 1 页）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymupdf

from app.models import Profile
from app.services import babeldoc_runner
from app.services.babeldoc_runner import RunnerOptions
from babeldoc.format.pdf.translation_config import TranslationConfig


def patch_config(cfg: TranslationConfig, **opts):
    for k, v in opts.items():
        setattr(cfg, k, v)
    return cfg


EXPERIMENTS = {
    # 补丁后：上游 PR #617/#618 修复 + 默认排版选项
    "E5a-patched-default": {"split_short_lines": False, "min_text_length": 5},
    # 补丁后：沿用 E4 选项（应用当前默认）
    "E5b-patched-E4": {},
}


async def run_one(profile: Profile, pdf: Path, pages: str, name: str, opts: dict):
    out_dir = Path("data/exp") / name
    cfg = babeldoc_runner.build_translation_config(
        profile,
        RunnerOptions(input_file=pdf, output_dir=out_dir,
                      lang_in="en", lang_out="zh", pages=pages),
    )
    patch_config(cfg, **opts)
    result = None
    async for ev in babeldoc_runner.translate_stream(cfg):
        if ev["type"] == "finish":
            result = ev["result"]
    assert result and result.mono_pdf_path, name
    mono = pymupdf.open(result.mono_pdf_path)
    mono[0].get_pixmap(dpi=110).save(f"data/exp/{name}.png")
    print(f"[ok] {name}")


async def main():
    from app.db import init_db

    init_db()  # 确保旧库补齐新列
    pdf = Path(sys.argv[1])
    pages = sys.argv[2] if len(sys.argv) > 2 else "1"
    only = set(sys.argv[3:]) or None  # 可选：只跑指定实验名
    profile = Profile(id=0, name="exp",
                      base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                      model="qwen3.8-max", qps=4)
    from sqlmodel import Session
    from app.db import engine
    from app.models import Profile as P

    with Session(engine) as s:
        src = s.get(P, 2)
        profile.base_url = src.base_url
        profile.api_key = src.api_key
        profile.model = src.model
    Path("data/exp").mkdir(parents=True, exist_ok=True)
    for name, opts in EXPERIMENTS.items():
        if only and name not in only:
            continue
        try:
            await run_one(profile, pdf, pages, name, opts)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {name}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
