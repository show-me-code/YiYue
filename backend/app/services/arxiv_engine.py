"""arXiv 源码直译引擎：ID 检测、源码下载/解包、主文件定位。"""

import gzip
import logging
import re
import shutil
import tarfile
import time
from pathlib import Path

import httpx
import pymupdf

from app import config
from app.services import textio

logger = logging.getLogger(__name__)

EPRINT_URL = "https://arxiv.org/e-print/{id}"
_UA = {"User-Agent": "yiyue/0.1 (local academic translation tool)"}
# 实测本机到 arXiv 的 e-print 下载可能耗时 2~3 分钟（4MB 源码包），
# 超时设短会让任务静默回退到 BabelDOC 排版——所以给足时间。
DOWNLOAD_TIMEOUT_S = 600.0
# 源码本地缓存：同一篇论文重复翻译时不再走网络（迭代调试时尤其重要）
CACHE_DIR = config.DATA_DIR / "arxiv-cache"


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


def download_pdf(arxiv_id: str) -> bytes:
    """下载论文原版 PDF（仅提供 ID、无上传文件时使用）。"""
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=DOWNLOAD_TIMEOUT_S,
                              follow_redirects=True) as client:
                r = client.get(url, headers=_UA)
                r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise ArxivEngineError("下载的内容不是 PDF")
            return r.content
        except ArxivEngineError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("download arxiv pdf attempt %s failed: %s",
                           attempt + 1, e)
    raise ArxivEngineError(f"arXiv PDF 下载失败：{last_err}")


def cached_source_path(arxiv_id: str) -> Path:
    """源码缓存的存放路径（按 ID 规整文件名）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", arxiv_id)
    return CACHE_DIR / safe / "source.bin"


def fresh_dir(task_dir: Path, name: str, tries: int = 5) -> Path:
    """返回一个可安全写入的目录。

    先尝试清理同名旧目录；若被占用（Windows 上杀软/索引器会短暂持有刚写入的
    目录，实测 rmtree 报 WinError 32），就换用带序号的新目录，而不是让整条
    arXiv 路线失败并静默回退到 BabelDOC 排版。
    """
    task_dir = Path(task_dir)
    for i in range(tries):
        cand = task_dir / name if i == 0 else task_dir / f"{name}_{i}"
        if not cand.exists():
            return cand
        try:
            shutil.rmtree(cand)
            return cand
        except OSError as e:
            logger.warning("清理 %s 失败（%s），改用一个新目录", cand, e)
    fallback = task_dir / f"{name}_{int(time.time())}"
    logger.warning("多次清理失败，使用 %s", fallback)
    return fallback


def download_source(arxiv_id: str, dest_dir: Path, use_cache: bool = True) -> Path:
    """下载 e-print 源码包到 dest_dir，返回本地文件路径。

    优先用本地缓存（同一篇论文重跑不必再等几分钟，也不受 arXiv 限流影响）；
    命中缓存时仍然照常落一份到任务目录，保持任务自包含。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "source.bin"

    cached = cached_source_path(arxiv_id)
    if use_cache and cached.exists() and cached.stat().st_size > 1024:
        shutil.copy(cached, path)
        logger.info("arXiv 源码命中本地缓存：%s（%d 字节）",
                    arxiv_id, cached.stat().st_size)
        return path

    url = EPRINT_URL.format(id=arxiv_id)
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=DOWNLOAD_TIMEOUT_S,
                              follow_redirects=True) as client:
                r = client.get(url, headers=_UA)
                r.raise_for_status()
            raw = r.content
            if raw[:2] == b"%P":  # arXiv 返回的是 PDF：说明无源码
                raise ArxivEngineError("该论文在 arXiv 上没有 LaTeX 源码（仅 PDF）")
            path.write_bytes(raw)
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, cached)
                logger.info("arXiv 源码已缓存：%s → %s", arxiv_id, cached)
            except OSError:  # 缓存失败不影响本次任务
                logger.warning("写入源码缓存失败（忽略）：%s", cached, exc_info=True)
            return path
        except ArxivEngineError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("download arxiv source attempt %s failed: %s",
                           attempt + 1, e)
    raise ArxivEngineError(f"arXiv 源码下载失败：{last_err}")


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _safe_tar_member(m: tarfile.TarInfo, dest_dir: Path) -> bool:
    """判断 tar 成员能否安全解包到 dest_dir。

    只判 `..` 是不够的：Windows 上 `C:/evil`、`\\\\server\\share` 这类成员名在
    `os.path.join` 里会**丢掉前面的目标目录**，直接写到盘符根；符号链接/硬链接
    还能把写入引到目标目录之外（tar 成员的 linkname 不受 name 检查约束）。
    """
    if m.issym() or m.islnk() or m.isdev():
        return False
    name = m.name.replace("\\", "/")
    if name.startswith("/") or _DRIVE_RE.match(name) or name.startswith("//"):
        return False
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False
    try:  # 双保险：解析后的真实路径必须仍落在目标目录内
        return (dest_dir / name).resolve().is_relative_to(dest_dir.resolve())
    except OSError:
        return False


def extract_source(src_file: Path, dest_dir: Path) -> list[Path]:
    """解包源码（tar.gz / tar / 单文件 gz tex），返回解出的 tex 文件列表。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw = src_file.read_bytes()
    texs: list[Path] = []
    try:
        with tarfile.open(fileobj=__import__("io").BytesIO(raw), mode="r:*") as t:
            safe_members = [m for m in t.getmembers()
                            if _safe_tar_member(m, dest_dir)]
            dropped = len(t.getmembers()) - len(safe_members)
            if dropped:
                logger.warning("源码包里有 %d 个危险成员（绝对路径/穿越/链接），已跳过",
                               dropped)
            try:
                t.extractall(dest_dir, members=safe_members, filter="data")
            except TypeError:  # Python < 3.12 没有 filter 参数
                t.extractall(dest_dir, members=safe_members)
            texs = [p for p in dest_dir.rglob("*.tex")]
    except tarfile.ReadError:
        # 单文件 gzip 的 tex
        try:
            text = textio.decode_source_bytes(gzip.decompress(raw))
        except Exception:
            text = textio.decode_source_bytes(raw)
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
    r"""按可能性排序候选主文件：main.tex 优先，其次 \input 数，再次体积。"""
    candidates = []
    for p in texs:
        try:
            text = textio.load_text(p)
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
            text = textio.load_text(cur)
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
            if p.with_suffix(".bbl").exists() or "thebibliography" in textio.load_text(p):
                return
        except OSError:
            continue
    logger.warning("源码中未见 .bbl/thebibliography，参考文献可能无法编译")


_BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\{([^{}]+)\}")
_INLINE_COMMENT_RE = re.compile(r"(?<!\\)%")


def _code_part(line: str) -> str:
    """行内第一个未转义 % 之前的部分（注释不参与检测与替换）。"""
    m = _INLINE_COMMENT_RE.search(line)
    return line[:m.start()] if m else line


def _bbl_target(name: str, main_tex: Path) -> Path:
    """\\bibliography{名} 对应的 .bbl 落盘位置（保留子目录，剔除越界路径）。"""
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        rel = Path(rel.name)
    return main_tex.parent / f"{rel}.bbl"


def _bbl_for(name: str, main_tex: Path, bbls: list[Path]) -> tuple[Path, str]:
    """为 \\bibliography{name} 准备一个可 \\input 的 .bbl，返回 (路径, 来源说明)。"""
    target = _bbl_target(name, main_tex)
    if target.exists():
        return target, "自带同名 .bbl"
    source = (
        next((b for b in bbls if b.stem == Path(name).name), None)
        or next((b for b in bbls if b.stem == main_tex.stem), None)
        or bbls[0]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, target)
    return target, source.name


def use_shipped_bbl(src_root: Path, main_tex: Path) -> dict:
    r"""把作者自带的已编译参考文献（.bbl）真正用起来（用户规格第 1 步）。

    arXiv 源码包里的 .bbl 常与 \bibliography{...} 的名字对不上（本文包的实例：
    main.tex 写 \bibliography{alain17a}，包里却只有 main.bbl），于是所有
    \cite 在编译后变成 (?) ——实测 35 处引用全部失效。改名还不够：Tectonic
    检测到 \bibliography 会自己跑 BibTeX，用不存在的 .bib 覆盖掉 .bbl。
    因此这里把 \bibliography{x} 直接改写为 \input{x.bbl}，引用即恢复。

    只在**非注释代码段**上替换：注释里的 `% \bibliography{old}` 若先出现，
    "只替换第一处"会把注释改掉、真正的命令原样留下，引用照样全变 (?)。
    返回 {"applied": bool, "reason": str, "mapping": [[x, 来源文件], ...]}
    """
    try:
        text = textio.load_text(main_tex)
    except OSError as e:
        return {"applied": False, "reason": f"主文件不可读：{e}", "mapping": []}

    lines = text.splitlines(keepends=True)
    codes = [_code_part(ln) for ln in lines]
    hits = [m for code in codes for m in _BIBLIOGRAPHY_RE.finditer(code)]
    if not hits:
        return {"applied": False, "reason": "未使用 \\bibliography", "mapping": []}
    bbls = sorted(p for p in src_root.rglob("*.bbl"))
    if not bbls:
        return {"applied": False, "reason": "源码包中没有 .bbl", "mapping": []}

    names: list[str] = []
    for m in hits:
        for n in m.group(1).split(","):
            n = n.strip()
            if n and n not in names:
                names.append(n)
    if not names:
        return {"applied": False, "reason": "\\bibliography 参数为空", "mapping": []}

    mapping: list[list[str]] = []
    for name in names:
        _, origin = _bbl_for(name, main_tex, bbls)
        mapping.append([name, origin])

    def rewrite(m: re.Match) -> str:
        parts = []
        for n in (x.strip() for x in m.group(1).split(",")):
            if not n:
                continue
            target = _bbl_target(n, main_tex)
            # \input 的路径必须和 .bbl 实际落盘位置一致（子目录名不能丢）
            parts.append("\\input{" + target.relative_to(main_tex.parent).as_posix()
                         + "}")
        return "".join(parts) or m.group(0)

    new_lines = []
    for ln, code in zip(lines, codes):
        if "\\bibliography" in code:
            new_lines.append(_BIBLIOGRAPHY_RE.sub(rewrite, code) + ln[len(code):])
        else:
            new_lines.append(ln)
    main_tex.write_text("".join(new_lines), "utf-8")
    logger.info("using authors' .bbl: %s", mapping)
    return {"applied": True, "reason": "", "mapping": mapping}
