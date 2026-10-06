"""闭环版式验证（设计规格第 5 步）。

1. 逐页渲染译文 PDF 为 PNG 供目检（data/verify/render/）；
2. PyMuPDF 逐页测量墨迹边界框（文本块+矢量+位图），与原版对应页版心对照，
   报告偏差超过阈值的页；
3. 对超阈值页输出对比明细，辅助区分"真问题"与提取假阳性。

用法：
    uv run python ../scripts/verify_pdf_layout.py <translated.pdf> <original.pdf> [--tol=25]
"""

import sys
from pathlib import Path

import pymupdf

OUT_DIR = Path("data/verify")


def ink_bbox(page: pymupdf.Page) -> pymupdf.Rect | None:
    bbox = None

    def merge(r):
        nonlocal bbox
        r = pymupdf.Rect(r)
        bbox = r if bbox is None else bbox | r

    for b in page.get_text("dict")["blocks"]:
        merge(b["bbox"])
    for dr in page.get_drawings():
        merge(dr["rect"])
    for img in page.get_image_info():
        merge(img["bbox"])
    return bbox


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tol = 25.0
    for a in sys.argv[1:]:
        if a.startswith("--tol="):
            tol = float(a.split("=", 1)[1])
    if len(args) < 2:
        sys.exit("用法: verify_pdf_layout.py <translated.pdf> <original.pdf> [--tol=25]")
    translated = pymupdf.open(args[0])
    original = pymupdf.open(args[1])

    render_dir = OUT_DIR / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    print(f"页面数：译文 {len(translated)} / 原文 {len(original)}")
    if len(translated) != len(original):
        print("!! 页数不一致 —— 需人工检查（同源重编译应完全一致）")

    flagged = []
    n = min(len(translated), len(original))
    for i in range(n):
        bt = ink_bbox(translated[i])
        bo = ink_bbox(original[i])
        if bt is None or bo is None:
            print(f"p{i+1}: 空白页或无法测量")
            continue
        deltas = [abs(bt.x0 - bo.x0), abs(bt.y0 - bo.y0),
                  abs(bt.x1 - bo.x1), abs(bt.y1 - bo.y1)]
        worst = max(deltas)
        status = "OK " if worst <= tol else "偏离"
        if worst > tol:
            flagged.append(i)
        print(f"p{i+1}: {status} 最大偏差 {worst:.1f}pt "
              f"(dx0={deltas[0]:.1f} dy0={deltas[1]:.1f} "
              f"dx1={deltas[2]:.1f} dy1={deltas[3]:.1f})")
        translated[i].get_pixmap(dpi=110).save(render_dir / f"p{i+1:02d}.png")

    print(f"\n渲染目录：{render_dir}（逐页目检）")
    if flagged:
        print(f"!! {len(flagged)} 页版心偏差超 {tol}pt："
              f"{[i+1 for i in flagged]} —— 放大对照这些页的原文/译文区域，"
              "区分真问题与提取假阳性")
    else:
        print("所有页版心偏差在阈值内，通过。")


if __name__ == "__main__":
    main()
