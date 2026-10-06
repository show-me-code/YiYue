"""arXiv 源码直译引擎：ID 检测、源码下载/解包、主文件定位。"""

import gzip
import logging
import re
import tarfile
from pathlib import Path

import httpx
import pymupdf

logger = logging.getLogger(__name__)

EPRINT_URL = "https://arxiv.org/e-print/{id}"
_UA = {"User-Agent": "yiyue/0.1 (local academic translation tool)"}


class ArxivEngineError(RuntimeError):
    pass


def detect_arxiv_id(filename: str = "", pdf_path: str | Path | None = None,
                    user_input: str = "") -> str | None:
    """从用户输入 / 文件名 / PDF 首页文本中检测 arXiv ID。

    支持 2401.12345、2401.12345v3、cs/0112017 以及 arxiv.org 各类链接。
    返回规范化 ID（含版本号若有），找不到返回 None。
    """
    text = (user_input or "") + " " + (filename or "")
    pat = re.compile(
        r"arxiv\.org/(?:abs|pdf|format)?/?([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?"
        r"|(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?!\d)"
        r"|(?<![\w/])([a-z\-]+/\d{7})(?!\d)",
        re.IGNORECASE,
    )
    for m in pat.finditer(text):
        gid = m.group(1) or m.group(3) or m.group(5)
        ver = m.group(2) or m.group(4) or ""
        if gid:
            return gid.lower() + ver.lower()

    if pdf_path:
        try:
            doc = pymupdf.open(pdf_path)
            head = "\n".join(doc[i].get_text() for i in range(min(2, len(doc))))
            doc.close()
            m = re.search(r"arXiv[:\s]*([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?",
                          head, re.IGNORECASE)
            if m:
                return (m.group(1) + (m.group(2) or "")).lower()
        except Exception:  # noqa: BLE001
            logger.debug("pdf text scan failed", exc_info=True)
    return None


def download_source(arxiv_id: str, dest_dir: Path) -> Path:
    """下载 e-print 源码包到 dest_dir，返回本地文件路径。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = EPRINT_URL.format(id=arxiv_id)
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                r = client.get(url, headers=_UA)
                r.raise_for_status()
            raw = r.content
            if raw[:2] == b"%P":  # arXiv 返回的是 PDF：说明无源码
                raise ArxivEngineError("该论文在 arXiv 上没有 LaTeX 源码（仅 PDF）")
            path = dest_dir / "source.bin"
            path.write_bytes(raw)
            return path
        except ArxivEngineError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("download arxiv source attempt %s failed: %s",
                           attempt + 1, e)
    raise ArxivEngineError(f"arXiv 源码下载失败：{last_err}")


def extract_source(src_file: Path, dest_dir: Path) -> list[Path]:
    """解包源码（tar.gz / tar / 单文件 gz tex），返回解出的 tex 文件列表。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw = src_file.read_bytes()
    texs: list[Path] = []
    try:
        with tarfile.open(fileobj=__import__("io").BytesIO(raw), mode="r:*") as t:
            safe_members = []
            for m in t.getmembers():
                # 防路径穿越
                if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts:
                    continue
                safe_members.append(m)
            t.extractall(dest_dir, members=safe_members)
            texs = [p for p in dest_dir.rglob("*.tex")]
    except tarfile.ReadError:
        # 单文件 gzip 的 tex
        try:
            text = gzip.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        if "\\documentclass" in text or "\\begin{document}" in text:
            out = dest_dir / "main.tex"
            out.write_text(text, "utf-8")
            texs = [out]
        else:
            raise ArxivEngineError("源码包无法解包且不是 LaTeX 文件")
    if not texs:
        raise ArxivEngineError("源码包中没有 .tex 文件")
    return texs


def rank_main_tex(texs: list[Path]) -> list[Path]:
    """按可能性排序候选主文件：main.tex 优先，其次 \input 数，再次体积。"""
    candidates = []
    for p in texs:
        try:
            text = p.read_text("utf-8", errors="replace")
        except OSError:
            continue
        if "\\documentclass" in text and "\\begin{document}" in text:
            n_inputs = len(re.findall(r"\\(?:input|include)\{", text))
            name_bonus = 2 if p.name.lower() == "main.tex" else 0
            candidates.append((name_bonus, n_inputs, len(text), p))
    candidates.sort(reverse=True)
    return [c[3] for c in candidates]


def find_main_tex(texs: list[Path]) -> Path:
    r"""定位主文件：含 \documentclass 且 \begin{document}；main.tex 优先。"""
    ranked = rank_main_tex(texs)
    if not ranked:
        raise ArxivEngineError("未找到包含 \\documentclass 的主 tex 文件")
    return ranked[0]


def collect_doc_files(main_tex: Path, texs: list[Path]) -> list[Path]:
    """主文件 + 递归 \\input/\\include 引用到的本地 tex 文件。"""
    by_stem: dict[str, Path] = {}
    for p in texs:
        by_stem.setdefault(p.stem, p)
        by_stem.setdefault(p.name, p)
    seen: set[Path] = {main_tex}
    order = [main_tex]
    stack = [main_tex]
    while stack:
        cur = stack.pop()
        try:
            text = cur.read_text("utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"\\(?:input|include)\{([^{}]+)\}", text):
            name = m.group(1).strip()
            target = by_stem.get(name) or by_stem.get(Path(name).stem)
            if target and target not in seen:
                seen.add(target)
                order.append(target)
                stack.append(target)
    return order


def compile_ready_check(texs: list[Path]) -> None:
    """基本检查：存在 bbl 或 内嵌 thebibliography（保证参考文献可编译）。"""
    for p in texs:
        try:
            if p.with_suffix(".bbl").exists() or "thebibliography" in p.read_text(
                "utf-8", errors="replace"
            ):
                return
        except OSError:
            continue
    logger.warning("源码中未见 .bbl/thebibliography，参考文献可能无法编译")
