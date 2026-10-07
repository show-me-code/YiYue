"""LaTeX 源码翻译管线：保护 → 分块翻译 → 恢复。

设计要点：
- 只翻译正文文本；公式、引用命令、图表环境、参考文献、注释一律不动。
- 保护采用占位符替换（[[M0]]…），恢复时对 [[M 12]] 之类的变形容错。
- 分块按空行段落聚合，单块 ≤ CHUNK_CHARS；单块翻译失败保留原文，不拖垮整篇。
"""

import concurrent.futures
import logging
import os
import re
from fractions import Fraction
from pathlib import Path

from app.services import textio

logger = logging.getLogger(__name__)

CHUNK_CHARS = 2500

# 并发翻译的块数。实测单块请求要 1~2 分钟（思考型模型 + 长提示词），串行翻一篇
# 60KB 的论文要 1.5~2 小时；这条路径的请求彼此独立，并发是唯一的墙钟杠杆。
# 全局 qps 由 BabelDOC 的 RateLimiter 兜着，加大并发不会突破配置档的速率上限。
TRANSLATE_WORKERS = max(1, int(os.environ.get("YIYUE_TRANSLATE_WORKERS", "4")))

# 单块翻译的墙钟上限（秒），可用环境变量覆盖。
# BabelDOC 的 do_translate 对 RateLimitError 会重试最多 100 次、退避最长 15s
# （最坏 ≈25 分钟），HTTP 客户端超时还是 600s/次。真跑 2607.03728 时整篇卡在
# 某一块上 25 分钟没有任何进展、进程 CPU 也不涨——用户只看到"翻译中"永远不动。
# 到点放弃这一块、保留原文继续：整篇至少能出成品，并把超时计入验证报告，不静默。
CHUNK_TIMEOUT_S = float(os.environ.get("YIYUE_CHUNK_TIMEOUT_S", "300"))


class ChunkTimeout(RuntimeError):
    """单块翻译超过墙钟上限（区别于普通调用失败）。"""


def _call_translator(translator, text: str, ignore_cache: bool = False,
                     timeout: float | None = None) -> str:
    """调用 translator.translate，带墙钟上限，避免一个卡死的请求拖死整篇。"""
    timeout = CHUNK_TIMEOUT_S if timeout is None else timeout
    kwargs = {"ignore_cache": True} if ignore_cache else {}
    if not timeout or timeout <= 0:
        return translator.translate(text, **kwargs)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(lambda: translator.translate(text, **kwargs))
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as e:
            if timeout >= 10:
                # 把当时所有线程的栈打到日志：真跑 2607.03728 时只看到"进度不动"、
                # 进程 CPU 也不涨，无法定位卡在哪一层。下次再卡就有栈可查。
                try:
                    import faulthandler

                    faulthandler.dump_traceback()
                except Exception:  # noqa: BLE001
                    pass
            raise ChunkTimeout(f"单块翻译超过 {timeout:.0f}s 未返回") from e
    finally:
        # 不等被放弃的请求：它可能还要跑几分钟，但整篇不能陪它一起等
        pool.shutdown(wait=False)

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
# 只认 M 命名空间：protected 列表只属于 M；[[C#]] 是图注/标题占位符，
# 由 put_caps 单独替换（见 _restore 的说明）。
_PLACEHOLDER_M_RE = re.compile(r"\[\[\s*M\s*(\d+)\s*\]\]")

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


_CAPTION_TOKEN_RE = re.compile(r"\[\[\s*C\s*(\d+)\s*\]\]")


def _extract_translatable_blocks(text: str, prefix: str = "C",
                                 cmds: tuple[str, ...] = _TITLE_CMDS,
                                 start_index: int = 0,
                                 ) -> tuple[str, list[str]]:
    r"""把 \caption / \captionof / \title 的内容抽成 [[C#]] 占位符，待单独翻译。

    用花括号配平解析而不是正则：图注里常有 \textbf{a \emph{b}} 这类嵌套，
    也有 \captionof{figure}{...}（caption 宏包）——正则版本会把它们整条漏掉，
    表现为图注静默保留英文。

    start_index 让调用方把多段抽取（正文图注、导言区标题）编进**同一个编号空间**：
    两段各自从 [[C0]] 起编号会互相覆盖，实测 \title 会被第一个图注顶掉。
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
        out.append(f"[[{prefix}{len(collected) + start_index}]]")
        collected.append(content)
        last = end  # 右花括号留给下一段
        continue
    out.append(text[last:])
    return "".join(out), collected


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
    #    命令名后用 (?![A-Za-z]) 而不是 \b：\b 在 `\citep*` 的 `*` 之后不成立，
    #    正则会退回去只匹配 `\citep`，星号被 [^{}\s%]* 吃掉、后面的 {key} 就留在
    #    正文里被当成散文翻译（引用键被改坏）。
    cmd_names = (
        r"cite[tp]?\*?|ref|eqref|autoref|Cref|label|url|href|bibliographystyle|"
        r"bibliography|includegraphics\*?|input|include|usepackage|"
        r"documentclass|bibitem|newcommand|renewcommand|renewcommand|def|"
        r"graphicspath|definecolor|setlength|setcounter|noindent|centering|"
        r"captionsetup|hspace\*?|vspace\*?|includeonly|tikzset|pgfplotsset"
    )
    out = re.sub(
        r"\\(?:" + cmd_names + r")(?![A-Za-z])(?:\[[^\]]*\])*(?:\{[^{}]*\}|[^{}\s%]*)",
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
    r"""把 [[M#]] 恢复为原片段（只处理 M 命名空间）。

    三条硬约束，每条都踩过坑：

    1. 只替换占位符本身，**绝不吞掉两侧的花括号**：源码里 `\hbox{[[M2]]}`
       的花括号是真实 LaTeX 语法，随手一起替换会产出 `\hbox$x$` 这种
       "Missing { inserted." 的坏文件（实测导致整节编译失败并退回英文原文）。
    2. **迭代到不再变化**：保护是分步做的，后面的步骤会把已含占位符的文本再包一层
       （注释行整行受保护时，行内 `\citep{key}` 先变成 [[M0]]，整行又变成 [[M1]]，
       而 M1 的内容就是 "% [[M0]]"）。单遍替换会把内层 [[M0]] 原样留在成品里——
       实测 appendix.tex/experiments.tex 里就有几十处。
    3. **不能碰 [[C#]]**：`protected` 是 M 列表，用下标去查 C 占位符会命中**完全
       无关**的受保护片段。迭代展开把这个问题从"恰好没触发"变成必然：实测
       `\caption{[[C7]]}` 被换成了一整段 `\begin{equation*}…\end{figure}`，
       编译报 `File ended while scanning use of \caption@ydblarg`，整篇回退。
       C 占位符留给后面的 put_caps。
    """
    def sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(protected):
            return protected[idx]
        return m.group(0)

    for _ in range(12):  # 深度上限：坏数据造成的自引用不至于死循环
        new = _PLACEHOLDER_M_RE.sub(sub, text)
        if new == text:
            return text
        text = new
    return text


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


def lang_display_name(lang_out: str) -> str:
    """目标语言的英文名（写进提示词用）。"""
    return _LANG_NAMES.get((lang_out or "").strip().lower(), "Simplified Chinese")


def system_prompt_for(lang_out: str, custom: str | None = None) -> str:
    """组装送给模型的总提示词：配置档自定义规范（若有）+ 占位符/结构规则。

    为什么必须显式送：arXiv 路径直接调 `translator.translate()`，BabelDOC 在
    `do_translate` 里用的是**写死的极简提示词**（"You are a professional,authentic
    machine translation engine."）——配置档里的自定义翻译规范、术语表、以及
    下面这套占位符规则全都不会到达模型。模型不知道 `[[M12]]` 必须原样保留，
    只能靠事后校验 + 绕缓存重试兜住（实测确实会被拒回）。
    """
    rules = SYSTEM_PROMPT.replace("{lang_out_name}", lang_display_name(lang_out))
    custom = (custom or "").strip()
    return (custom + "\n\n" + rules) if custom else rules


def _integrity_ok(src: str, dst: str) -> bool:
    r"""校验译文块未破坏 LaTeX 结构。

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


def _merge_stats(target: dict, extra: dict) -> None:
    """把子任务自己收集的统计并进总统计（数字相加、列表拼接）。"""
    for key, value in extra.items():
        if isinstance(value, bool):
            target[key] = value
        elif isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value
        elif isinstance(value, list):
            target.setdefault(key, []).extend(value)
        else:
            target[key] = value


def _map_parallel(worker, items: list, stats: dict | None, *,
                  workers: int | None = None, progress_cb=None,
                  progress_base: int = 0, progress_total: int | None = None,
                  stage: str = "翻译正文") -> list:
    """并发执行 `worker(item) -> (结果, 局部统计)`，返回值顺序与输入一致。

    为什么并发：单块请求实测 1~2 分钟（思考型模型 + 长提示词），串行翻一篇 60KB
    的论文要一两个钟头。各块彼此独立，BabelDOC 的 `TranslationCache`（对
    "database is locked" 有兜底）与 `RateLimiter`（带锁）都是线程安全的，
    并发只是把总墙钟除以并发度；速率上限仍由全局限流器兜住。

    两条约束：
    - **进度回调只在主线程发**：`_set_progress` 要写 SQLite + 广播，不能多线程写库；
    - **取消要立刻生效**：回调抛异常（TaskCancelled）时取消未开始的块并立即返回，
      不等排队中的块跑完（否则取消一次要等好几分钟）。
    """
    if not items:
        return []
    workers = max(1, workers or TRANSLATE_WORKERS)
    total = progress_total if progress_total is not None else len(items)
    results: list = [None] * len(items)
    if workers == 1:
        for i, item in enumerate(items):
            results[i], local = worker(item)
            if stats is not None:
                _merge_stats(stats, local)
            if progress_cb:
                progress_cb(stage, progress_base + i + 1, total)
        return results

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(worker, item) for item in items]
        for i, future in enumerate(futures):
            results[i], local = future.result()
            if stats is not None:
                _merge_stats(stats, local)
            if progress_cb:
                progress_cb(stage, progress_base + i + 1, total)
    except BaseException:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=False)
    return results


def _chunk_worker(translator, is_caption: bool):
    """构造正文块任务：统计用**局部字典**收集，回主线程再合并。

    `_translate_chunk` 里是 `stats[key] = stats.get(key, 0) + 1`，多线程直接共享
    一个 dict 会丢计数。
    """
    def worker(chunk: str):
        local: dict = {}
        return (_translate_chunk(translator, chunk, local, is_caption=is_caption),
                local)
    return worker


def _caption_worker(translator):
    """构造图注/标题任务：内部先分块翻译，再恢复并按源文校结构。"""
    def worker(cap: str):
        local: dict = {}
        try:
            cap_protected, cap_protection = _build_protection(cap)
            pieces = []
            for chunk in _iter_chunks(cap_protected):
                piece = _translate_chunk(translator, chunk, local, is_caption=True)
                pieces.append(piece if piece is not None else chunk)
            restored = _restore(" ".join(pieces), cap_protection)
            if _structure_counts(restored) == _structure_counts(cap):
                return restored, local
            logger.warning("caption structure broken after restore, keep source")
            local["captions_untranslated"] = \
                local.get("captions_untranslated", 0) + 1
            return None, local
        except Exception as e:  # noqa: BLE001
            logger.warning("caption translate failed, keep original: %s", e)
            local["captions_untranslated"] = \
                local.get("captions_untranslated", 0) + 1
            return None, local
    return worker


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
            piece = _call_translator(translator, protected)
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
    # 必须按起始位置**从后往前**改：edits 里章节标题先收集、子图注后收集，
    # 直接 reversed() 只是把收集顺序倒过来，并不等于位置倒序，前面的改动会把
    # 后面记录的偏移量顶偏，替换到错误位置。
    for start, stop, new in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + new + text[stop:]
    return text

def _looks_untranslated(src: str, dst: str) -> bool:
    """译文与原文几乎一致，且原文含足量英文单词 → 判为未翻译（多为历史坏缓存）。

    只比较**可翻译文本**（去掉占位符）：公式、命令、引用在原文与译文里逐字相同，
    混进来会把"公式密集但正文确实翻了"的块算成高相似度——2500 字符里只有几十个
    英文单词的块相似度轻松 >0.97，译文就被无谓地退回英文。判定改成两条：
    去掉占位符后逐字相同，或**英文单词数几乎没有减少**（译文是中文时英文单词必然
    大幅消失）。
    """
    if _english_word_count(src) < 5:
        return False
    src_prose = _PLACEHOLDER_RE.sub("", src)
    dst_prose = _PLACEHOLDER_RE.sub("", dst)
    if dst_prose.strip() == src_prose.strip():
        return True
    n_src = _english_word_count(src_prose)
    n_dst = _english_word_count(dst_prose)
    if n_src and n_dst >= 0.8 * n_src:
        return True
    import difflib

    return difflib.SequenceMatcher(None, src_prose, dst_prose).ratio() > 0.97


def _translate_chunk(translator, chunk: str, stats: dict | None = None,
                     is_caption: bool = False) -> str | None:
    """翻译单块：占位符/结构校验失败或"原样返回"时绕过缓存重试一次。

    返回 None 表示两次都不合格，调用方回退原文（并计入 stats 供闭环报告）。
    """
    key_fail = "captions_untranslated" if is_caption else "chunks_untranslated"
    key_retry = "identity_retries"
    key_timeout = "captions_timeout" if is_caption else "chunks_timeout"
    n_src = len(_PLACEHOLDER_RE.findall(chunk))

    def acceptable(text: str) -> bool:
        return (len(_PLACEHOLDER_RE.findall(text)) == n_src
                and _integrity_ok(chunk, text)
                and not _looks_untranslated(chunk, text))

    try:
        candidate = _call_translator(translator, chunk)
    except ChunkTimeout as e:
        # 超时不再重试：第一次就卡了 5 分钟，重试只会再等一个上限
        logger.warning("chunk translate timed out, keep original: %s", e)
        if stats is not None:
            stats[key_timeout] = stats.get(key_timeout, 0) + 1
            stats[key_fail] = stats.get(key_fail, 0) + 1
        return None
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
        retry = _call_translator(translator, chunk, ignore_cache=True)
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
    # 导言区的 \title 接在正文图注之后编号：两段各自从 0 起会撞号，
    # 实测 put_caps 会把 \title 的位置替换成正文第一个图注（标题被静默吃掉）。
    preamble, pre_caps = _extract_translatable_blocks(
        preamble, "C", ("title",), start_index=len(body_caps))
    all_caps = body_caps + pre_caps

    # 2) 命令/数学/环境保护
    protected_text, protected = _build_protection(body)
    chunks = list(_iter_chunks(protected_text))
    if stats is not None:
        stats["chunks_total"] = stats.get("chunks_total", 0) + len(chunks)
        stats["captions_total"] = stats.get("captions_total", 0) + len(all_caps)
    out_chunks = []
    n_jobs = len(chunks) + len(all_caps)
    body_results = _map_parallel(
        _chunk_worker(translator, False), chunks, stats,
        progress_cb=progress_cb, progress_total=n_jobs, stage="翻译正文",
    )
    for i, (chunk, translated) in enumerate(zip(chunks, body_results)):
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

    new_body = "\n\n".join(out_chunks)

    # 3) 翻译 caption / title 内容（同样并发；每个图注一个任务）
    cap_results = _map_parallel(
        _caption_worker(translator), all_caps, stats,
        progress_cb=progress_cb, progress_base=len(chunks),
        progress_total=n_jobs, stage="翻译图表标题",
    )
    for j, restored in enumerate(cap_results):
        if restored is not None:
            all_caps[j] = restored

    def put_caps(text: str) -> str:
        # 用正则容忍 LLM 写出的 [[C 4]] / [[ C4 ]] 这类变体，否则会把
        # 占位符原样留在成品 PDF 里（纯文本替换只认 [[C4]] 这种精确写法）
        def sub(m: re.Match) -> str:
            idx = int(m.group(1))
            return all_caps[idx] if 0 <= idx < len(all_caps) else m.group(0)
        return _CAPTION_TOKEN_RE.sub(sub, text)

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


# xcolor 被显式带选项加载的写法（选项要与后面那次加载完全一致才能消除冲突）
_XCOLOR_USE_RE = re.compile(r"\\usepackage\[([^\]]*)\]\{xcolor\}")
# 日志里点名了冲突发生在哪个文件的第几行：优先从那个文件取选项
_XCOLOR_CLASH_LOG_RE = re.compile(
    r"([\w./\\ -]+?):\d+: LaTeX Error: Option clash for package xcolor")


def _xcolor_options(log_text: str, tex: str, src_root: Path | None) -> str:
    """找出作者给 xcolor 指定的选项（修复必须用同一组，否则仍然冲突）。"""
    candidates: list[Path] = []
    if src_root:
        m = _XCOLOR_CLASH_LOG_RE.search(log_text)
        if m:
            stem = Path(m.group(1).strip()).name
            candidates += [p for p in src_root.rglob(f"{stem}*") if p.is_file()]
        candidates += sorted(src_root.rglob("*.tex"))
        candidates += sorted(src_root.rglob("*.sty"))
    for f in candidates:
        try:
            mm = _XCOLOR_USE_RE.search(textio.load_text(f))
        except OSError:
            continue
        if mm and mm.group(1).strip():
            return mm.group(1).strip()
    mm = _XCOLOR_USE_RE.search(tex)
    return mm.group(1).strip() if mm else ""


def repair_from_log(tex: str, log_text: str,
                    src_root: Path | None = None) -> str | None:
    """根据编译日志对主 tex 做针对性修复；无已知修复点返回 None。

    两类修复：

    1. **subfigure 环境未定义**（源码用了 subcaption 的环境却只装了冲突的 subfig
       宏包——arXiv 上不少论文带这种隐患）：不能用 subcaption 修补（与 subfig
       冲突），改用 minipage 垫片模拟 subfigure 环境，插在 \\documentclass 之后。
    2. **xcolor 选项冲突**：源码先由某个 .sty（经 eso-pic）**无选项**加载 xcolor，
       之后又写 `\\usepackage[table,dvipsnames]{xcolor}`，在旧版 LaTeX（Tectonic 内置
       的是 2021 版；作者 00README.json 指定的是 TeX Live 2025 + pdflatex）下直接
       报 `Option clash for package xcolor`，**源码本身就无法编译**（实测原始源码
       不加任何处理也是这个错）。用 `\\PassOptionsToPackage` 把作者自己写的那组选项
       提前到最早的加载点，冲突消失且作者要的 `table`/`dvipsnames` 功能照旧。
    """
    before = ""   # 必须放在 \documentclass 之前
    after = ""    # 必须放在 \documentclass 之后
    if ("Option clash for package xcolor" in log_text
            and "PassOptionsToPackage" not in tex):
        opts = _xcolor_options(log_text, tex, src_root)
        if opts:
            before = f"\\PassOptionsToPackage{{{opts}}}{{xcolor}}\n"
    if ("Environment subfigure undefined" in log_text
            and "newenvironment{subfigure}" not in tex):
        after = _SUBFIGURE_SHIM
    if not before and not after:
        return None
    m = re.search(r"(?m)^(\\documentclass(?:\[[^\]]*\])?\{[^{}]*\}[^\n]*)$", tex)
    if not m:
        return None
    tail = ("\n" + after) if after else ""
    return tex[:m.start()] + before + tex[m.start():m.end()] + tail + tex[m.end():]


# ---- 预处理：字体与 XeTeX 兼容 ----

# 汉化块放在 \begin{document} 之前，晚于作者自己的 \renewcommand{\figurename}…
# 否则作者重定义会把我们的汉化覆盖回英文（实测 main.tex 第 39 行就是这么干的）。
_CTEX_LINE = "\\usepackage[fontset=fandol]{ctex}\n"
# ctex 会放大行距（实测 12 页变 14 页），这里改成**可控**：先记录文档原本的
# \baselinestretch，再按任务选项放大 line_spread 倍。
# 中文（Fandol）字形在同一字号下比 Times 大得多，沿用英文原版的 10.95pt 行距会显得
# 很挤；中文排版惯例是 1.3 倍左右，所以默认放大，同时允许用户选"跟原文一致"。
# 注意：Tectonic 自带的 LaTeX 是 2021 版，**没有 \fpeval**（实测直接
# "Undefined control sequence" 导致整条 arXiv 路线失败回退），所以用 e-TeX 原生
# \dimexpr 做定点乘法：\strip@pt\dimexpr <原值>\p@ * <分子> / <分母>。
_CTEX_STRETCH_KEEP = (
    "\\makeatletter\n"
    "\\edef\\yiyuekeptstretch{\\baselinestretch}\n"
    "\\ifx\\yiyuekeptstretch\\empty\\edef\\yiyuekeptstretch{1}\\fi\n"
    "\\edef\\yiyuespread{\\strip@pt\\dimexpr\\yiyuekeptstretch\\p@ * %d / %d\\relax}\n"
    "\\makeatother\n"
    + _CTEX_LINE +
    "\\linespread{\\yiyuespread}\\selectfont\n"
)
# 自动标签的简/繁两套写法：繁体任务下把「图/参考文献」写成简体是明显的疏漏
_LOCALIZE_WORDS = {
    "zh": {"figure": "图", "table": "表", "ref": "参考文献", "abstract": "摘要",
           "keywords": "关键词", "appendix": "附录"},
    "zt": {"figure": "圖", "table": "表", "ref": "參考文獻", "abstract": "摘要",
           "keywords": "關鍵詞", "appendix": "附錄"},
}


def _lang_family(lang_out: str) -> str:
    """把 zh-CN / zh_CN / zt 之类规整成 zh / zt / 其它。"""
    lang = (lang_out or "").strip().lower().replace("_", "-")
    return lang.split("-")[0]


def needs_ctex(lang_out: str) -> bool:
    """目标语言是否需要 ctex（简/繁中文）。"""
    return _lang_family(lang_out) in ("zh", "zt")


def _localize_words(lang_out: str) -> dict:
    return _LOCALIZE_WORDS["zt" if _lang_family(lang_out) == "zt" else "zh"]


def label_localize(lang_out: str) -> str:
    """按目标语言生成自动标签汉化块（简/繁）。

    用字符串拼接而不是 %-格式化：模板里的 `%\\n`（防止行尾空格被 TeX 吃掉）
    会被当成格式化占位符。
    """
    w = _localize_words(lang_out)
    return (
        "\\providecommand\\figurename{}\\providecommand\\tablename{}"
        "\\providecommand\\refname{}\\providecommand\\abstractname{}"
        "\\providecommand\\keywordsname{}\\providecommand\\appendixname{}"
        "\\providecommand\\equationname{}\\providecommand\\sectionname{}%\n"
        "\\renewcommand\\figurename{" + w["figure"] + "}"
        "\\renewcommand\\tablename{" + w["table"] + "}"
        "\\renewcommand\\refname{" + w["ref"] + "}"
        "\\renewcommand\\abstractname{" + w["abstract"] + "}"
        "\\renewcommand\\keywordsname{" + w["keywords"] + "}"
        "\\renewcommand\\appendixname{" + w["appendix"] + "}%\n"
    )


# 向后兼容：默认简体块
_LABEL_LOCALIZE = label_localize("zh")


def prepare_for_xetex(tex: str, lang_out: str = "zh", line_spread: float = 1.0) -> str:
    """中文重编译预处理（用户规格第 3 步）：

    - 用 ctex 宏包（Fandol 字体集）处理中文断行、标点禁则、中西文间距，
      叠加在原样式之上；不依赖系统字体（Fandol 随 TeX 发行版分发）；
      **简体（zh）与繁体（zt）都要注入**——繁体也是中文，之前只认 zh 前缀，
      zt 任务完全不注 ctex（中文没有断行与禁则支持）；
    - **行距可调**：line_spread=1.0 与原文一致；中文排版惯例约 1.3（默认由任务选项传入）。
      实现为「文档原本的 \\baselinestretch × line_spread」，不会把作者自己的行距设置抹掉；
    - 移除 XeTeX 下冲突的 inputenc/fontenc；
    - 汉化自动标签：图/表/参考文献/附录/摘要等（简繁各自用字），且放在导言区末尾执行，
      不会被作者自己的 \\renewcommand 覆盖。
    样式文件里硬编码的 "Abstract" 由 patch_style_files 单独处理。
    """
    out = tex
    out = re.sub(r"(?m)^\\usepackage\[(?:[^\]]*)\]\{inputenc\}.*\n?", "", out)
    out = re.sub(r"(?m)^\\usepackage\[(?:[^\]]*)\]\{fontenc\}.*\n?", "", out)
    if not needs_ctex(lang_out) or "\\documentclass" not in out:
        return out
    spread = max(1.0, min(float(line_spread or 1.0), 2.0))
    num, den = Fraction(spread).limit_denominator(100).as_integer_ratio()
    if "fontset=fandol" not in out:
        out = re.sub(
            r"(?m)^(\\documentclass(?:\[[^\]]*\])?\{[^{}]*\}[^\n]*)$",
            lambda m: m.group(1) + "\n" + (_CTEX_STRETCH_KEEP % (num, den)),
            out,
            count=1,
        )
    # 汉化块：插在 \begin{document} 之前（导言区最后），作者的重定义已经执行完
    if "\\begin{document}" in out:
        marker = "\\renewcommand\\figurename{%s}" % _localize_words(lang_out)["figure"]
        if marker not in out:
            out = out.replace("\\begin{document}",
                              label_localize(lang_out) + "\\begin{document}", 1)
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
                # 无损读取：errors="replace" 得到的 U+FFFD 会随写回固化进样式文件
                text = textio.load_text(f)
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
