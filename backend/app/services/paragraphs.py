"""逐段对照阅读：从原文/译文 PDF 抽取正文段落并按版面位置对齐。

抽取口径：按页遍历文本块（pymupdf blocks，阅读序排序），合并块内换行，
过滤页眉页脚/孤立编号等碎块（有效字符数不足阈值的块）。
对齐口径：
1. 过滤 arXiv 打戳行等只存在于 PDF 包装层的碎块；
2. 锚点配对——两边逐字相同的块（作者、邮箱、未翻译的参考文献条目）按位置就近强制配对，
   把全文切成若干锚点区间；
3. 区间内按相对版面位置 rel = (页序号 + 块顶纵坐标/页高) / 总页数 单调贪心配对。
相比"按序号硬对齐"，它能容忍两侧块数不同（译文页数更少、首页标题/作者块拆分不同）
而不发生错位。
结果按任务目录缓存（paragraphs.json），译文 PDF 变化（增量合并）时自动重建。
"""

import json
import logging
import re
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

_MIN_LETTERS = 20  # 有效字符数阈值：低于它的块视为页眉/页脚/编号碎块
_WS = re.compile(r"\s+")
_LETTER = re.compile(r"[0-9A-Za-z一-鿿]")
_STAMP = re.compile(r"^arXiv:\s*\d")  # arXiv 打戳行只在原版 PDF 上，译文没有


def _letter_count(text: str) -> int:
    return len(_LETTER.findall(text))


def _clean(text: str) -> str:
    # 块内换行合并为空格；行尾连字符（英文断词）直接接合
    text = re.sub(r"-\n", "", text)
    return _WS.sub(" ", text.replace("\n", " ")).strip()


def extract_paragraphs(pdf_path: str | Path) -> list[dict]:
    """抽取正文段落，返回 [{"page": 1, "rel": 0.02, "text": "..."}]。

    page 为 1-based；rel 为块在整篇文档中的相对版面位置（0~1）。
    标题等会被排成多个短块的场景，先把"同列、纵向紧邻、且含短行"的相邻块合并，
    避免短块被阈值过滤后标题整段丢失。
    """
    out: list[dict] = []
    with pymupdf.open(pdf_path) as doc:
        n = max(len(doc), 1)
        for pno in range(len(doc)):
            page = doc[pno]
            ph = max(page.rect.height, 1.0)
            blocks = [b for b in page.get_text("blocks", sort=True)
                      if len(b) >= 7 and b[6] == 0]
            merged: list[list] = []
            for b in blocks:
                if merged:
                    p = merged[-1]
                    gap = b[1] - p[3]  # 下一块顶 - 上一块底
                    overlap = min(p[2], b[2]) - max(p[0], b[0])
                    width = max(min(p[2] - p[0], b[2] - b[0]), 1.0)
                    short = _letter_count(_clean(p[4])) < _MIN_LETTERS \
                        or _letter_count(_clean(b[4])) < _MIN_LETTERS
                    if short and 0 <= gap < 30 and overlap / width > 0.5:
                        p[3] = max(p[3], b[3])
                        p[2] = max(p[2], b[2])
                        p[4] += "\n" + b[4]
                        continue
                merged.append([b[0], b[1], b[2], b[3], b[4]])
            for b in merged:
                text = _clean(b[4])
                if _letter_count(text) < _MIN_LETTERS or _STAMP.match(text):
                    continue
                out.append({
                    "page": pno + 1,
                    "rel": (pno + b[1] / ph) / n,
                    "text": text,
                })
    return out


def _positional(src: list[dict], tgt: list[dict]) -> list[tuple[dict, dict]]:
    """区间内按相对版面位置单调贪心配对。"""
    out: list[tuple[dict, dict]] = []
    j = 0
    for s in src:
        if j >= len(tgt):
            break
        # 单调推进：下一个译文块位置更接近时就前进
        while j + 1 < len(tgt) \
                and abs(tgt[j + 1]["rel"] - s["rel"]) <= abs(tgt[j]["rel"] - s["rel"]):
            j += 1
        out.append((s, tgt[j]))
        j += 1
    return out


def align(src: list[dict], tgt: list[dict]) -> list[dict]:
    """锚点分段 + 位置配对，返回 [{"i","page","source","target"}]。"""
    # 锚点：两边逐字相同的块（作者/邮箱/未翻译条目），按位置就近占用
    by_text: dict[str, list[int]] = {}
    for j, t in enumerate(tgt):
        by_text.setdefault(t["text"], []).append(j)
    used = [False] * len(tgt)
    anchors: list[tuple[int, int]] = []
    for i, s in enumerate(src):
        cands = [j for j in by_text.get(s["text"], []) if not used[j]]
        if cands:
            j = min(cands, key=lambda jj: abs(tgt[jj]["rel"] - s["rel"]))
            used[j] = True
            anchors.append((i, j))

    raw: list[tuple[dict, dict]] = []
    bounds = [(-1, -1), *anchors, (len(src), len(tgt))]
    for (i0, j0), (i1, j1) in zip(bounds, bounds[1:]):
        raw.extend(_positional(src[i0 + 1:i1], tgt[j0 + 1:j1]))
    # 锚点本身也成对（译文=原文，如作者姓名/参考文献条目）
    raw.extend((src[i], tgt[j]) for i, j in anchors)
    raw.sort(key=lambda r: r[0]["rel"])
    return [{"i": k, "page": s["page"], "source": s["text"], "target": t["text"]}
            for k, (s, t) in enumerate(raw)]


def _sig(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, int(st.st_mtime)


def load_pairs(task_dir: str | Path) -> dict:
    """加载（或重建）某任务的段落对齐结果。

    arXiv 任务（有作者源码）走 tex_paragraphs 的源码级抽取：保留 LaTeX 公式与
    引用标记，段落边界即源码空行；其余任务走 PDF 文本块抽取 + 位置对齐。
    返回 {"ready": bool, "mode": "tex"|"pdf", "macros": {...}, "pairs": [...], ...}。
    """
    task_dir = Path(task_dir)
    original = task_dir / "original.pdf"
    mono = task_dir / "mono.pdf"
    if not original.exists() or not mono.exists():
        return {"ready": False, "pairs": [], "source_count": 0, "target_count": 0}

    cache_file = task_dir / "paragraphs.json"
    sig = _sig(mono)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text("utf-8"))
            # 版本号随抽取/对齐算法升级，避免旧算法的结果被当成有效缓存
            if cached.get("v") == 10 and tuple(cached.get("mono_sig", ())) == sig:
                return cached["data"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # 缓存损坏则重建

    from app.services import tex_paragraphs
    data = tex_paragraphs.tex_pairs(task_dir)
    if data is None:
        src = extract_paragraphs(original)
        tgt = extract_paragraphs(mono)
        data = {
            "ready": True,
            "mode": "pdf",
            "macros": {},
            "pairs": align(src, tgt),
            "source_count": len(src),
            "target_count": len(tgt),
        }
    try:
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": 10, "mono_sig": sig, "data": data},
                                  ensure_ascii=False), "utf-8")
        tmp.replace(cache_file)
    except OSError:  # 缓存写失败不影响返回
        logger.warning("段落缓存写入失败：%s", cache_file, exc_info=True)
    return data


def context_text(task_dir: str | Path, max_chars: int = 24000) -> str:
    """供 AI 问答使用的论文正文上下文（取原文段落，截断到 max_chars）。"""
    task_dir = Path(task_dir)
    original = task_dir / "original.pdf"
    if not original.exists():
        return ""
    paragraphs = extract_paragraphs(original)
    text = "\n\n".join(p["text"] for p in paragraphs)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[……正文过长，已截断]"
    return text
