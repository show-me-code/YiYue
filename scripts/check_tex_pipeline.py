"""无 LLM 的确定性全管线检查：真实源码 → 翻译管线 → Tectonic 编译。

为什么需要它：tex 管线（抽取 → 保护 → 恢复 → put_caps → ctex 注入 → .bbl 改写）
的损坏往往只在**真跑一篇论文**时才暴露，而真跑要花 LLM token 和时间。
用回声翻译器替代 LLM 就能在几秒到一分钟内复现结构性问题——实测靠它定位到
"M/C 命名空间混淆导致 `\\caption{[[C7]]}` 被替换成无关 LaTeX 片段"。

用法：
    cd backend
    uv run python ../scripts/check_tex_pipeline.py                       # 自动取最近任务的 arxiv_src
    uv run python ../scripts/check_tex_pipeline.py --src data/tasks/8/arxiv_src
    uv run python ../scripts/check_tex_pipeline.py --src path/to/src --keep   # 保留中间产物

退出码：0 = 编译通过且无占位符泄漏；1 = 有问题（细节打印在最后）。
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import (arxiv_engine, tectonic, tex_translate,  # noqa: E402
                          textio)

PLACEHOLDER_RE = re.compile(r"\[\[\s*[MC]\s*\d+\s*\]\]")


class EchoTranslator:
    """回声翻译器：逐行加"译"前缀，不花 token，也不改结构。

    **必须保留换行**：真实 LLM 被要求"不要增删空行"，这里也一样——否则
    `%` 注释会把同一行的后续内容整段吞掉（而 `\\bibliography` 一旦被注释吞掉，
    `.bbl` 就不会生效），检查结果会指向一个真实 LLM 不会造成的假象。
    """

    def translate(self, text, ignore_cache=False):
        return "\n".join("译" + ln if ln.strip() else ln
                         for ln in text.split("\n"))


def guess_src() -> Path | None:
    tasks = ROOT / "backend" / "data" / "tasks"
    if not tasks.exists():
        return None
    for d in sorted(tasks.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        for name in ("arxiv_src", "arxiv_out"):
            cand = d / name
            if cand.exists() and any(cand.rglob("*.tex")):
                return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=None,
                    help="论文源码目录（默认取最近任务的 arxiv_src）")
    ap.add_argument("--out", type=Path, default=ROOT / "backend/data/_pipeline_check")
    ap.add_argument("--lang", default="zh", help="目标语言（zh/zt）")
    ap.add_argument("--line-spread", type=float, default=1.3)
    ap.add_argument("--keep", action="store_true", help="保留中间产物目录")
    args = ap.parse_args()

    src = args.src or guess_src()
    if not src or not Path(src).exists():
        print("找不到源码目录，请用 --src 指定", file=sys.stderr)
        return 2
    src = Path(src).resolve()
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(src, out)
    for stale in ("main.pdf", "main.aux", "main.log", "compile.log"):
        (out / stale).unlink(missing_ok=True)

    texs = [p for p in out.rglob("*.tex")]
    main_tex = arxiv_engine.find_main_tex(texs)
    files = arxiv_engine.collect_doc_files(main_tex, texs)
    print(f"src : {src}")
    print(f"main: {main_tex.name}")
    print(f"files: {[f.name for f in files]}")

    tex_translate.patch_style_files(out)
    stats: dict = {}
    for f in files:
        text = textio.load_text(f)
        translated = tex_translate.translate_tex(
            text, EchoTranslator(), args.lang, None, stats)
        translated = tex_translate.prepare_for_xetex(
            translated, args.lang, args.line_spread)
        textio.save_text(f, translated)

    leaked: list[tuple[str, int, str]] = []
    for f in files:
        raw = textio.load_text(f)
        for m in PLACEHOLDER_RE.finditer(raw):
            leaked.append((f.name, raw[:m.start()].count("\n") + 1, m.group(0)))

    info = arxiv_engine.use_shipped_bbl(out, main_tex)
    print(f"bbl : {info}")
    print(f"占位符泄漏: {len(leaked)} 处")
    for item in leaked[:10]:
        print("   ", item)
    if stats:
        print(f"统计: {stats}")

    ok = True
    try:
        pdf = tectonic.compile_tex(main_tex, out)
        print(f"编译: OK -> {pdf}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"编译: FAILED -> {str(e)[:400]}")
        log = out / "main.log"
        if log.exists():
            for ln in textio.load_text(log).splitlines():
                if ln.startswith("!") or "Runaway" in ln or ln.startswith("l."):
                    print("   LOG:", ln[:180])
    if leaked:
        ok = False
        print("=> 有占位符漏进成品源码，管线未清理干净")

    if not args.keep and ok:
        shutil.rmtree(out, ignore_errors=True)
    else:
        print(f"中间产物保留在：{out}")
    print("结论：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
