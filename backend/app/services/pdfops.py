"""PDF 切分/合并/页数，基于 pypdf。"""

from pathlib import Path

from pypdf import PdfReader, PdfWriter


def page_count(path: str | Path) -> int:
    return len(PdfReader(path).pages)


def split_pdf(src: str | Path, out_dir: str | Path, part_size: int) -> list[Path]:
    """把 PDF 切成若干 part_0001.pdf…，返回按序路径列表。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(src)
    n = len(reader.pages)
    parts: list[Path] = []
    for idx, start in enumerate(range(0, n, part_size)):
        writer = PdfWriter()
        for j in range(start, min(start + part_size, n)):
            writer.add_page(reader.pages[j])
        part_path = out_dir / f"part_{idx:04d}.pdf"
        with open(part_path, "wb") as f:
            writer.write(f)
        parts.append(part_path)
    return parts


def merge_pdfs(paths: list[str | Path], out: str | Path) -> None:
    """按顺序合并 PDF；先写临时文件再原子替换。"""
    writer = PdfWriter()
    for p in paths:
        writer.append(str(p))
    out = Path(out)
    tmp = out.with_suffix(".merging.pdf")
    with open(tmp, "wb") as f:
        writer.write(f)
    tmp.replace(out)


def parse_page_ranges(expr: str) -> list[tuple[int, int]]:
    """解析 "1,2,1-,-3,3-5" 形式的页码表达式为 1-based 闭区间列表。

    开端缺失表示从第 1 页起，末端缺失表示到最后一页。
    """
    ranges: list[tuple[int, int]] = []
    for token in expr.replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" not in token:
            a = int(token)
            ranges.append((a, a))
            continue
        left, _, right = token.partition("-")
        a = int(left) if left.strip() else 1
        b = int(right) if right.strip() else 10**9
        if a > b:
            raise ValueError(f"无效页码区间：{token}")
        ranges.append((a, b))
    if not ranges:
        raise ValueError("页码表达式为空")
    return ranges
