"""LaTeX 源码翻译管线：保护 → 分块翻译 → 恢复。

设计要点：
- 只翻译正文文本；公式、引用命令、图表环境、参考文献、注释一律不动。
- 保护采用占位符替换（[[M0]]…），恢复时对 [[M 12]] 之类的变形容错。
- 分块按空行段落聚合，单块 ≤ CHUNK_CHARS；单块翻译失败保留原文，不拖垮整篇。
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CHUNK_CHARS = 2500

# 整体保护的环境（内部完全不动）
_PROTECT_ENVS = [
    "verbatim", "verbatim*", "lstlisting", "minted",
    "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*",
    "figure", "figure*", "table", "table*", "tabular*", "wraptable",
    "thebibliography", "algorithm", "algorithmic", "algorithm2e",
    "split", "cases", "matrix", "pmatrix", "bmatrix", "array", "subequations",
]

_PLACEHOLDER_RE = re.compile(r"\[\[\s*([MC])\s*(\d+)\s*\]\]")

# \caption[opt]{content}（content 允许一层花括号嵌套）
_CAPTION_RE = re.compile(r"(\\caption\*?(?:\[[^\]]*\])?\{)((?:[^{}]|\{[^{}]*\})*)\}")
_TITLE_RE = re.compile(r"(\\title\*?(?:\[[^\]]*\])?\{)((?:[^{}]|\{[^{}]*\})*)\}")


def _extract_translatable_blocks(text: str, pattern: re.Pattern,
                                 prefix: str) -> tuple[str, list[str]]:
    """把 \\caption{...}/\\title{...} 的内容抽出为 [[C#]] 占位符，待单独翻译。"""
    collected: list[str] = []

    def repl(m: re.Match) -> str:
        idx = len(collected)
        collected.append(m.group(2))
        return m.group(1) + f"[[{prefix}{idx}]]" + "}"

    return pattern.sub(repl, text), collected


def _restore_all(text: str, protected: list[str]) -> str:
    return _restore(text, protected)


def _build_protection(tex: str) -> tuple[str, list[str]]:
    """把不可翻译的片段替换为 [[M#]] 占位符，返回 (新文本, 受保护片段列表)。"""
    protected: list[str] = []

    def protect(match_text: str) -> str:
        idx = len(protected)
        protected.append(match_text)
        return f"[[M{idx}]]"

    out = tex

    # 1) 整段环境（最长环境名优先，避免 align 匹配到 alignat 的前缀问题）
    for env in sorted(_PROTECT_ENVS, key=len, reverse=True):
        pat = re.compile(
            r"(?s)\\begin\{" + re.escape(env) + r"\}.*?\\end\{" + re.escape(env) + r"\}"
        )
        out = pat.sub(lambda m: protect(m.group(0)), out)

    # 2) 显示数学
    out = re.sub(r"(?s)\\\[(.*?)\\\]", lambda m: protect(m.group(0)), out)
    out = re.sub(r"(?s)\$\$(.*?)\$\$", lambda m: protect(m.group(0)), out)

    # 3) 不可翻译命令（含可选参数与花括号参数，支持一层嵌套）
    cmd_names = (
        r"cite[tp]?\*?|ref|eqref|autoref|Cref|label|url|href|bibliographystyle|"
        r"bibliography|includegraphics\*?|input|include|usepackage|"
        r"documentclass|bibitem|newcommand|renewcommand|renewcommand|def|"
        r"graphicspath|definecolor|setlength|setcounter|noindent|centering|"
        r"captionsetup|hspace\*?|vspace\*?|includeonly|tikzset|pgfplotsset"
    )
    out = re.sub(
        r"\\(?:" + cmd_names + r")\b(?:\[[^\]]*\])*(?:\{[^{}]*\}|[^{}\s%]*)",
        lambda m: protect(m.group(0)),
        out,
    )

    # 4) 行内数学 $...$（跳过 \$；无嵌套要求，非贪婪）
    out = re.sub(
        r"(?<![\\$])\$(?!\$)(?:\\.|[^$\\])+?\$",
        lambda m: protect(m.group(0)),
        out,
    )

    # 5) 注释行整行保护（含 % 注释），保留原样
    out = re.sub(
        r"(?m)^[ \t]*%.*$",
        lambda m: protect(m.group(0)),
        out,
    )
    # 行尾注释（% 前不是 \）
    out = re.sub(
        r"(?m)(?<!\\)(%.*)$",
        lambda m: protect(m.group(0)),
        out,
    )
    return out, protected


def _restore(text: str, protected: list[str]) -> str:
    """把 [[M#]] / [[C#]] / [[ M# ]] / {[M#]} 等变形恢复为原片段。"""
    def sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(protected):
            return protected[idx]
        return m.group(0)

    text = re.sub(r"\{\[\[\s*[MC]\s*(\d+)\s*\]\]\}", sub, text)
    text = re.sub(r"\[\[\s*[MC]\s*(\d+)\s*\]\]", sub, text)
    return text


def _iter_chunks(body: str):
    """按空行分段聚合为 ≤CHUNK_CHARS 的翻译块。"""
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    buf: list[str] = []
    size = 0
    for p in paras:
        if size and size + len(p) > CHUNK_CHARS:
            yield "\n\n".join(buf)
            buf, size = [], 0
        buf.append(p)
        size += len(p)
    if buf:
        yield "\n\n".join(buf)


SYSTEM_PROMPT = (
    "You are a professional academic paper translator. You receive fragments of "
    "a LaTeX document. Translate ONLY the English prose into {lang_out_name}. "
    "Rules: 1) Keep every LaTeX command, brace structure, placeholder token like "
    "[[M12]] or [[C3]], math, punctuation of commands EXACTLY unchanged. 2) Do not add or "
    "remove blank lines; keep paragraph breaks. 3) Translate section/subsection "
    "titles' text too. 4) Output ONLY the translated fragment, no explanations."
)

_LANG_NAMES = {"zh": "Simplified Chinese", "zt": "Traditional Chinese",
               "en": "English", "ja": "Japanese"}


def _integrity_ok(src: str, dst: str) -> bool:
    """校验译文块未破坏 LaTeX 结构。

    - 全局：花括号与 $ 总数一致；
    - 占位符邻位：源文中紧贴占位符的 { 和 } 必须在译文中原位保留
      （实测 LLM 最典型的损伤是 \hbox{[[M1]]} → \hbox[[M1]] 吞掉邻位花括号，
      即使在别处补偿使总数不变也会被此检查捕获）。
    """
    if dst.count("{") != src.count("{") or dst.count("}") != src.count("}"):
        return False
    if dst.count("$") != src.count("$"):
        return False
    for m in _PLACEHOLDER_RE.finditer(src):
        tok = m.group(0)
        i = dst.find(tok)
        if i == -1:
            return False
        prev_s = src[m.start() - 1] if m.start() > 0 else ""
        prev_d = dst[i - 1] if i > 0 else ""
        nxt_s = src[m.end()] if m.end() < len(src) else ""
        nxt_d = dst[i + len(tok)] if i + len(tok) < len(dst) else ""
        if (prev_s == "{") != (prev_d == "{"):
            return False
        if (nxt_s == "}") != (nxt_d == "}"):
            return False
    return True


def translate_tex(
    tex: str,
    translator,  # 具备 .translate(text) 的 BabelDOC 翻译器
    lang_out: str = "zh",
    progress_cb=None,
) -> str:
    """翻译一份 .tex 的正文；preamble 与 document 环境标记原样保留。

    图表 \\caption 与 \\title 的内容单独成块翻译（环境本身受保护）。
    progress_cb(stage, done, total) 汇报进度。
    """
    lang_name = _LANG_NAMES.get(lang_out, "Simplified Chinese")
    m = re.search(r"(?s)^(.*?)(\\begin\{document\})(.*?)(\\end\{document\})(.*)$",
                  tex)
    if not m:
        # 非 body 文件（被 \input 的片段）：整文件按正文处理
        preamble, begin, body, end, tail = "", "", tex, "", ""
    else:
        preamble, begin, body, end, tail = m.groups()

    # 1) 抽出 caption / title 内容（含 preamble 中的 title）
    body, body_caps = _extract_translatable_blocks(body, _CAPTION_RE, "C")
    preamble, pre_caps = _extract_translatable_blocks(preamble, _TITLE_RE, "C")
    all_caps = body_caps + pre_caps

    # 2) 命令/数学/环境保护
    protected_text, protected = _build_protection(body)
    chunks = list(_iter_chunks(protected_text))
    out_chunks = []
    for i, chunk in enumerate(chunks):
        translated = None
        try:
            candidate = translator.translate(chunk)
            # 占位符数量校验：LLM 吞掉/改写占位符时回退原文
            n_src = len(_PLACEHOLDER_RE.findall(chunk))
            n_out = len(_PLACEHOLDER_RE.findall(candidate))
            if n_src != n_out:
                logger.warning("chunk %s placeholder mismatch %s→%s, revert",
                               i, n_src, n_out)
            elif not _integrity_ok(chunk, candidate):
                logger.warning("chunk %s brace/math integrity check failed, revert",
                               i)
            else:
                translated = candidate
        except Exception as e:  # noqa: BLE001
            logger.warning("chunk %s translate failed, keep original: %s", i, e)
        out_chunks.append(_restore(translated, protected) if translated is not None
                          else _restore(chunk, protected))
        if progress_cb:
            progress_cb("翻译正文", i + 1, len(chunks) + len(all_caps))

    new_body = "\n\n".join(out_chunks)

    # 3) 翻译 caption / title 内容
    for j, cap in enumerate(all_caps):
        try:
            cap_protected, cap_protection = _build_protection(cap)
            pieces = []
            for chunk in _iter_chunks(cap_protected):
                piece = translator.translate(chunk)
                if (len(_PLACEHOLDER_RE.findall(piece)) != len(
                    _PLACEHOLDER_RE.findall(chunk)
                ) or not _integrity_ok(chunk, piece)):
                    piece = chunk
                pieces.append(piece)
            all_caps[j] = _restore(" ".join(pieces), cap_protection)
        except Exception as e:  # noqa: BLE001
            logger.warning("caption %s translate failed, keep original: %s", j, e)
        if progress_cb:
            progress_cb("翻译图表标题", len(chunks) + j + 1,
                        len(chunks) + len(all_caps))

    def put_caps(text: str) -> str:
        for j, cap in enumerate(all_caps):
            text = text.replace(f"[[C{j}]]", cap)
        return text

    new_body = put_caps(new_body)
    preamble = put_caps(preamble)
    return f"{preamble}{begin}\n{new_body}\n{end}{tail}"


# ---- 编译失败修复 ----

_SUBFIGURE_SHIM = (
    "\\makeatletter\n"
    "\\newenvironment{subfigure}[2][]{"
    "\\begin{minipage}[t]{#2}\\def\\@captype{figure}}"
    "{\\end{minipage}}\n"
    "\\makeatother\n"
)


def repair_from_log(tex: str, log_text: str) -> str | None:
    """根据编译日志对主 tex 做针对性修复；无已知修复点返回 None。

    subfigure 环境未定义（源码用了 subcaption 的环境却只装了冲突的 subfig
    宏包——arXiv 上不少论文带这种隐患）：不能用 subcaption 修补（与 subfig
    冲突），改用 minipage 垫片模拟 subfigure 环境。
    """
    inject = ""
    if "Environment subfigure undefined" in log_text and "newenvironment{subfigure}" not in tex:
        inject = _SUBFIGURE_SHIM
    if not inject:
        return None
    m = re.search(r"(?m)^(\\documentclass(?:\[[^\]]*\])?\{[^{}]*\}[^\n]*)$", tex)
    if not m:
        return None
    return tex[:m.end()] + "\n" + inject + tex[m.end():]


# ---- 预处理：字体与 XeTeX 兼容 ----

def prepare_for_xetex(tex: str, lang_out: str = "zh") -> str:
    """中文重编译预处理（用户规格第 3 步）：

    - 用 ctex 宏包（Fandol 字体集）处理中文断行、标点禁则、中西文间距，
      叠加在原样式之上；不依赖系统字体（Fandol 随 TeX 发行版分发）；
    - 移除 XeTeX 下冲突的 inputenc/fontenc；
    - 汉化自动标签：图/表/参考文献/附录/摘要等。
    样式文件里硬编码的 "Abstract" 由 patch_style_files 单独处理。
    """
    out = tex
    out = re.sub(r"(?m)^\\usepackage\[(?:[^\]]*)\]\{inputenc\}.*\n?", "", out)
    out = re.sub(r"(?m)^\\usepackage\[(?:[^\]]*)\]\{fontenc\}.*\n?", "", out)
    if lang_out.startswith("zh") and "ctex" not in out and "\\documentclass" in out:
        inject = (
            "\\usepackage[fontset=fandol]{ctex}\n"
            "\\renewcommand\\figurename{图}\n"
            "\\renewcommand\\tablename{表}\n"
            "\\renewcommand\\refname{参考文献}\n"
            "\\renewcommand\\abstractname{摘要}\n"
            "\\renewcommand\\keywordsname{关键词}\n"
            "\\renewcommand\\appendixname{附录}\n"
        )
        out = re.sub(
            r"(?m)^(\\documentclass(?:\[[^\]]*\])?\{[^{}]*\}[^\n]*)$",
            lambda m: m.group(1) + "\n" + inject,
            out,
            count=1,
        )
    return out


def patch_style_files(src_root: Path) -> int:
    """把 .sty/.cls 里硬编码的 "Abstract" 标题补丁为 "摘要"（返回修改文件数）。"""
    changed = 0
    for pattern in ("*.sty", "*.cls"):
        for f in src_root.rglob(pattern):
            try:
                text = f.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if "Abstract" not in text:
                continue
            new = re.sub(r"(\{)Abstract(\})", r"\g<1>摘要\g<2>", text)
            if new != text:
                f.write_text(new, "utf-8")
                changed += 1
    return changed
