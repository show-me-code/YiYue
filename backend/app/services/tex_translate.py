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

# 需要单独成块翻译的标题类命令
_TITLE_CMDS = ("caption", "captionof", "subcaption", "title")
_CMD_HEAD_RE = re.compile(r"\\(captionof|caption|subcaption|subfloat|title)\*?")


def _match_braced(text: str, pos: int) -> tuple[str, int] | None:
    """pos 指向 '{'，返回 (内容, 右花括号下标)；支持任意层嵌套与 \\{ 转义。"""
    if pos >= len(text) or text[pos] != "{":
        return None
    depth = 0
    i = pos
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1:i], i
        i += 1
    return None


def _match_bracketed(text: str, pos: int) -> tuple[str, int] | None:
    """pos 指向 '['，返回 (内容, 右方括号下标)；用于 \\subfloat[子图注]{...}。"""
    if pos >= len(text) or text[pos] != "[":
        return None
    depth = 0
    i = pos
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[pos + 1:i], i
        i += 1
    return None


def _extract_translatable_blocks(text: str, prefix: str = "C",
                                 cmds: tuple[str, ...] = _TITLE_CMDS,
                                 ) -> tuple[str, list[str]]:
    r"""把 \caption / \captionof / \title 的内容抽成 [[C#]] 占位符，待单独翻译。

    用花括号配平解析而不是正则：图注里常有 \textbf{a \emph{b}} 这类嵌套，
    也有 \captionof{figure}{...}（caption 宏包）——正则版本会把它们整条漏掉，
    表现为图注静默保留英文。
    """
    collected: list[str] = []
    out: list[str] = []
    last = 0
    for m in _CMD_HEAD_RE.finditer(text):
        cmd = m.group(1)
        if cmd not in cmds:
            continue
        if m.start() < last:  # 落在上一条标题参数内部，跳过避免文本重叠错乱
            continue
        line_head = text.rfind("\n", 0, m.start()) + 1
        if text[line_head:m.start()].lstrip().startswith("%"):
            continue  # 注释掉的标题不动（注释行整体受保护）
        j = m.end()
        if j < len(text) and text[j] == "[":  # 可选短标题
            k = text.find("]", j)
            if k == -1:
                continue
            j = k + 1
        if cmd == "captionof":  # \captionof{figure}{...}
            while j < len(text) and text[j] in " \t":
                j += 1
            if j >= len(text) or text[j] != "{":
                continue
            r = _match_braced(text, j)
            if r is None:
                continue
            j = r[1] + 1
            while j < len(text) and text[j] in " \t":
                j += 1
        if j >= len(text) or text[j] != "{":
            continue
        matched = _match_braced(text, j)
        if matched is None:
            continue
        content, end = matched
        if not content.strip():
            continue
        out.append(text[last:j + 1])
        out.append(f"[[{prefix}{len(collected)}]]")
        collected.append(content)
        last = end  # 右花括号留给下一段
        continue
    out.append(text[last:])
    return "".join(out), collected


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
    """把 [[M#]] / [[C#]] / [[ M# ]] 恢复为原片段。

    只替换占位符本身，**绝不吞掉两侧的花括号**：源码里 `\\hbox{[[M2]]}`
    的花括号是真实 LaTeX 语法，随手一起替换会产出 `\\hbox$x$` 这种
    "Missing { inserted." 的坏文件（实测导致整节编译失败并退回英文原文）。
    """
    def sub(m: re.Match) -> str:
        idx = int(m.group(2))  # group(1) 是 M/C 标记，group(2) 才是序号
        if 0 <= idx < len(protected):
            return protected[idx]
        return m.group(0)

    return _PLACEHOLDER_RE.sub(sub, text)


def _structure_counts(text: str) -> tuple[int, int, int]:
    """花括号左/右与 $ 的计数，用于恢复后的一致性校验。"""
    return text.count("{"), text.count("}"), text.count("$")


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
    "titles' text too. 4) Output ONLY the translated fragment, no explanations. "
    "5) Translate label words that appear in the prose into Chinese as well "
    "(Figure/Fig. → 图, Table → 表, Section → 节, Equation/Eq. → 式, "
    "Appendix → 附录, References → 参考文献), keeping the following placeholder or "
    "number untouched. 6) Figure and table captions are prose: always translate them; "
    "never return the input unchanged."
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


def _english_word_count(text: str) -> int:
    """统计英文单词数（用于识别"等于没翻译"的返回）。"""
    return len(re.findall(r"[A-Za-z]{3,}", text))


# 章节类标题命令：LLM 偶尔漏译短标题（实测 \section{Introduction} 被原样留下）
_SECTION_CMD_RE = re.compile(
    r"\\(section|subsection|subsubsection|paragraph|chapter|part)\*?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _translate_english_titles(text: str, translator, stats: dict | None) -> str:
    """把仍是纯英文的章节标题与子图注做一次定点补译（正文块之外的兜底）。

    - 章节标题：LLM 偶尔把 \\section{Introduction} 整条漏掉，正文变成"1 Introduction"；
    - 子图注 \\subfloat[英文子图注]{...}：它在 figure 环境里受保护、不走 caption 通道，
      只能在译文出来后按"仍是纯英文"补一次。用方括号包裹的场合不引入占位符，
      避免 [[C1]] 里的 ']' 提前结束 LaTeX 可选参数。
    """
    edits: list[tuple[int, int, str]] = []

    def translate_label(text: str, start: int, end: int, min_words: int = 1) -> None:
        content = text[start:end]
        if not re.search(r"[A-Za-z]", content) or _CJK_RE.search(content):
            return
        if len(re.findall(r"[A-Za-z]{2,}", content)) < min_words:
            return  # 单词型子图注（如 "ResNet-50"）不值得再发一次请求
        protected, protection = _build_protection(content)
        try:
            piece = translator.translate(protected)
        except Exception as e:  # noqa: BLE001
            logger.warning("label translate failed: %s", e)
            return
        if (len(_PLACEHOLDER_RE.findall(piece)) != len(_PLACEHOLDER_RE.findall(protected))
                or not _integrity_ok(protected, piece)):
            return
        new = _restore(piece, protection).strip()
        if new and new != content.strip():
            edits.append((start, end, new))

    for m in _SECTION_CMD_RE.finditer(text):
        line_head = text.rfind("\n", 0, m.start()) + 1
        if text[line_head:m.start()].lstrip().startswith("%"):
            continue
        j = m.end()
        if j < len(text) and text[j] == "{":
            matched = _match_braced(text, j)
            if matched:
                translate_label(text, j + 1, matched[1])

    for m in re.finditer(r"\\subfloat\*?", text):
        j = m.end()
        while j < len(text) and text[j] in " \t":
            j += 1
        if j < len(text) and text[j] == "[":
            matched_b = _match_bracketed(text, j)
            if matched_b:
                translate_label(text, j + 1, matched_b[1], min_words=2)

    if stats is not None:
        stats["labels_retranslated"] = stats.get("labels_retranslated", 0) + len(edits)
    for start, stop, new in reversed(edits):
        text = text[:start] + new + text[stop:]
    return text

def _looks_untranslated(src: str, dst: str) -> bool:
    """译文与原文几乎一致，且原文含足量英文单词 → 判为未翻译（多为历史坏缓存）。"""
    if _english_word_count(src) < 5:
        return False
    if dst.strip() == src.strip():
        return True
    import difflib

    return difflib.SequenceMatcher(None, src, dst).ratio() > 0.97


def _translate_chunk(translator, chunk: str, stats: dict | None = None,
                     is_caption: bool = False) -> str | None:
    """翻译单块：占位符/结构校验失败或"原样返回"时绕过缓存重试一次。

    返回 None 表示两次都不合格，调用方回退原文（并计入 stats 供闭环报告）。
    """
    key_fail = "captions_untranslated" if is_caption else "chunks_untranslated"
    key_retry = "identity_retries"
    n_src = len(_PLACEHOLDER_RE.findall(chunk))

    def acceptable(text: str) -> bool:
        return (len(_PLACEHOLDER_RE.findall(text)) == n_src
                and _integrity_ok(chunk, text)
                and not _looks_untranslated(chunk, text))

    try:
        candidate = translator.translate(chunk)
    except Exception as e:  # noqa: BLE001
        logger.warning("chunk translate failed, keep original: %s", e)
        if stats is not None:
            stats[key_fail] = stats.get(key_fail, 0) + 1
        return None
    if acceptable(candidate):
        return candidate

    reason = "占位符/结构校验" if (len(_PLACEHOLDER_RE.findall(candidate)) != n_src
                              or not _integrity_ok(chunk, candidate)) else "原样返回（疑似坏缓存）"
    logger.info("%s check failed (%s), retry bypassing cache", "caption" if is_caption else "chunk", reason)
    if stats is not None:
        stats[key_retry] = stats.get(key_retry, 0) + 1
    try:
        retry = translator.translate(chunk, ignore_cache=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("chunk retry failed, keep original: %s", e)
        if stats is not None:
            stats[key_fail] = stats.get(key_fail, 0) + 1
        return None
    if acceptable(retry):
        return retry
    logger.warning("chunk still unacceptable after retry (%s), revert to source", reason)
    if stats is not None:
        stats[key_fail] = stats.get(key_fail, 0) + 1
    return None


def translate_tex(
    tex: str,
    translator,  # 具备 .translate(text) 的 BabelDOC 翻译器
    lang_out: str = "zh",
    progress_cb=None,
    stats: dict | None = None,
) -> str:
    """翻译一份 .tex 的正文；preamble 与 document 环境标记原样保留。

    图表 \\caption / \\captionof 与 \\title 的内容单独成块翻译（环境本身受保护）。
    progress_cb(stage, done, total) 汇报进度；stats 收集未翻译块计数供验证报告使用。
    """
    lang_name = _LANG_NAMES.get(lang_out, "Simplified Chinese")
    m = re.search(r"(?s)^(.*?)(\\begin\{document\})(.*?)(\\end\{document\})(.*)$",
                  tex)
    if not m:
        # 非 body 文件（被 \input 的片段）：整文件按正文处理
        preamble, begin, body, end, tail = "", "", tex, "", ""
    else:
        preamble, begin, body, end, tail = m.groups()

    # 1) 抽出 caption / captionof / subcaption / subfloat / title 内容（含 preamble 中的 title）
    body, body_caps = _extract_translatable_blocks(
        body, "C", ("caption", "captionof", "subcaption", "subfloat"))
    preamble, pre_caps = _extract_translatable_blocks(preamble, "C", ("title",))
    all_caps = body_caps + pre_caps

    # 2) 命令/数学/环境保护
    protected_text, protected = _build_protection(body)
    chunks = list(_iter_chunks(protected_text))
    if stats is not None:
        stats["chunks_total"] = stats.get("chunks_total", 0) + len(chunks)
        stats["captions_total"] = stats.get("captions_total", 0) + len(all_caps)
    out_chunks = []
    for i, chunk in enumerate(chunks):
        translated = _translate_chunk(translator, chunk, stats)
        source_chunk = _restore(chunk, protected)
        if translated is not None:
            final = _restore(translated, protected)
            # 恢复后仍需与源文结构一致：占位符两侧花括号若被吞掉，编译期才会爆
            # "Missing { inserted."，那时整节已被迫回退英文——这里提前拦住。
            if _structure_counts(final) != _structure_counts(source_chunk):
                logger.warning("chunk %s structure broken after restore, revert", i)
                if stats is not None:
                    stats["chunks_structure_reverted"] = \
                        stats.get("chunks_structure_reverted", 0) + 1
                final = source_chunk
        else:
            final = source_chunk
        out_chunks.append(final)
        if progress_cb:
            progress_cb("翻译正文", i + 1, len(chunks) + len(all_caps))

    new_body = "\n\n".join(out_chunks)

    # 3) 翻译 caption / title 内容
    for j, cap in enumerate(all_caps):
        try:
            cap_protected, cap_protection = _build_protection(cap)
            pieces = []
            for chunk in _iter_chunks(cap_protected):
                piece = _translate_chunk(translator, chunk, stats, is_caption=True)
                pieces.append(piece if piece is not None else chunk)
            restored = _restore(" ".join(pieces), cap_protection)
            if _structure_counts(restored) == _structure_counts(cap):
                all_caps[j] = restored
            else:
                logger.warning("caption %s structure broken after restore, keep source", j)
                if stats is not None:
                    stats["captions_untranslated"] = \
                        stats.get("captions_untranslated", 0) + 1
        except Exception as e:  # noqa: BLE001
            logger.warning("caption %s translate failed, keep original: %s", j, e)
            if stats is not None:
                stats["captions_untranslated"] = stats.get("captions_untranslated", 0) + 1
        if progress_cb:
            progress_cb("翻译图表标题", len(chunks) + j + 1,
                        len(chunks) + len(all_caps))

    def put_caps(text: str) -> str:
        for j, cap in enumerate(all_caps):
            text = text.replace(f"[[C{j}]]", cap)
        return text

    new_body = put_caps(new_body)
    preamble = put_caps(preamble)

    # 4) 章节标题补译：LLM 偶尔漏译 \section{Introduction} 这类短标题，
    #    表现为中英混排的"1 Introduction"。这里对仍是纯英文的标题做一次定点补译。
    new_body = _translate_english_titles(new_body, translator, stats)
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

# 汉化块放在 \begin{document} 之前，晚于作者自己的 \renewcommand{\figurename}…
# 否则作者重定义会把我们的汉化覆盖回英文（实测 main.tex 第 39 行就是这么干的）。
_CTEX_LINE = "\\usepackage[fontset=fandol]{ctex}\n"
# ctex 会放大行距（实测 12 页变 14 页，与"原样保留版式"冲突），记录原值并还原。
_CTEX_STRETCH_KEEP = (
    "\\edef\\yiyuekeptstretch{\\baselinestretch}\n"
    + _CTEX_LINE +
    "\\linespread{\\yiyuekeptstretch}\\selectfont\n"
)
_LABEL_LOCALIZE = (
    "\\providecommand\\figurename{}\\providecommand\\tablename{}"
    "\\providecommand\\refname{}\\providecommand\\abstractname{}"
    "\\providecommand\\keywordsname{}\\providecommand\\appendixname{}"
    "\\providecommand\\equationname{}\\providecommand\\sectionname{}%\n"
    "\\renewcommand\\figurename{图}\\renewcommand\\tablename{表}"
    "\\renewcommand\\refname{参考文献}\\renewcommand\\abstractname{摘要}"
    "\\renewcommand\\keywordsname{关键词}\\renewcommand\\appendixname{附录}%\n"
)


def prepare_for_xetex(tex: str, lang_out: str = "zh") -> str:
    """中文重编译预处理（用户规格第 3 步）：

    - 用 ctex 宏包（Fandol 字体集）处理中文断行、标点禁则、中西文间距，
      叠加在原样式之上；不依赖系统字体（Fandol 随 TeX 发行版分发）；
    - **还原 ctex 改动的行距**，保证版式与原版一致（页数不因注入而变）；
    - 移除 XeTeX 下冲突的 inputenc/fontenc；
    - 汉化自动标签：图/表/参考文献/附录/摘要等，且放在导言区末尾执行，
      不会被作者自己的 \\renewcommand 覆盖。
    样式文件里硬编码的 "Abstract" 由 patch_style_files 单独处理。
    """
    out = tex
    out = re.sub(r"(?m)^\\usepackage\[(?:[^\]]*)\]\{inputenc\}.*\n?", "", out)
    out = re.sub(r"(?m)^\\usepackage\[(?:[^\]]*)\]\{fontenc\}.*\n?", "", out)
    if not lang_out.startswith("zh") or "\\documentclass" not in out:
        return out
    already = "fontset=fandol" in out
    if not already:
        out = re.sub(
            r"(?m)^(\\documentclass(?:\[[^\]]*\])?\{[^{}]*\}[^\n]*)$",
            lambda m: m.group(1) + "\n" + _CTEX_STRETCH_KEEP,
            out,
            count=1,
        )
    # 汉化块：插在 \begin{document} 之前（导言区最后），作者的重定义已经执行完
    if "\\renewcommand\\figurename{图}" not in out and "\\begin{document}" in out:
        out = out.replace("\\begin{document}",
                          _LABEL_LOCALIZE + "\\begin{document}", 1)
    return out


# 样式文件里硬编码的摘要标题：常见三种写法
#   {\large\bf Abstract}   （nips_2016_nomarkings.sty 就是这个，原正则 \{Abstract\} 匹配不到）
#   \textbf{Abstract} / {Abstract} / \def\abstractname{Abstract}
_ABSTRACT_HEADING_PATTERNS = (
    (re.compile(r"(\\bfseries\s+|\\bf\s+)Abstract\b"), r"\g<1>摘要"),
    (re.compile(r"(\\textbf\{)Abstract(\})"), r"\g<1>摘要\g<2>"),
    (re.compile(r"(\{)Abstract(\})"), r"\g<1>摘要\g<2>"),
)


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
            new = text
            for pat, repl in _ABSTRACT_HEADING_PATTERNS:
                new = pat.sub(repl, new)
            if new != text:
                f.write_text(new, "utf-8")
                changed += 1
    return changed
