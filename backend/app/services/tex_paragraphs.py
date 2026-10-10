"""arXiv 任务的段落抽取：直接读 .tex 源码，保留行内/展示公式与引用。

与 PDF 文本块抽取（paragraphs.py）的差异：
- 公式保留为 LaTeX（$...$ / $$...$$），前端用 KaTeX 排版渲染；
- \\cite 家族按"全文首次出现顺序"编号渲染为 ⟦1,2⟧ 引用标记（无 .aux 可解析时的
  确定性口径，与数字顺序型文献样式一致）；
- \\ref/\\eqref 等交叉引用渲染为 ⟦§:label⟧ 标记（前端显示 § 角标，悬停见标签名）；
- 段落边界即 .tex 源码的空行——不会出现 PDF 文本块把一个段落截成两半的问题。

原文取 arxiv_src（作者源码），译文取 arxiv_out（翻译后源码）；两条目录的文件结构
一致，按相对路径配对文件、按段落序号配对段落。
"""

import difflib
import re
from pathlib import Path

from app.services import textio
from app.services.arxiv_engine import find_main_tex

# ---------- 注释与导言区 ----------

def _strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", text)


def _body(text: str) -> str:
    m = re.search(r"\\begin\{document\}(.*)\\end\{document\}", text, re.S)
    return m.group(1) if m else ""


# ---------- KaTeX 宏收集（作者自定义命令，如 \Rsq） ----------

_DEF_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\s*\{?\s*(\\[A-Za-z]+)\s*\}?"
    r"(?:\s*\[(\d)\])?\s*\{",
)


def _read_group(text: str, i: int) -> tuple[str, int]:
    """i 指向 '{'，返回 (内容, 紧接 '}' 之后的位置)。"""
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return text[i + 1:], len(text)


def collect_macros(tex_files: list[Path]) -> dict[str, str]:
    """从导言区收集零参/单参/双参宏定义，转成 KaTeX macros 格式。"""
    macros: dict[str, str] = {}
    for path in tex_files:
        try:
            text = textio.load_text(path)
        except OSError:
            continue
        preamble = text[:text.find("\\begin{document}")] \
            if "\\begin{document}" in text else text
        preamble = _strip_comments(preamble)
        for m in _DEF_RE.finditer(preamble):
            body, _ = _read_group(preamble, m.end() - 1)
            if not body.strip():
                continue
            # 我们的宏只在 $...$ 数学环境里用：剥掉 \ensuremath 壳，
            # 否则 KaTeX 在数学模式下收到 \ensuremath 会渲染失败
            body = re.sub(r"\\ensuremath\s*\{([^{}]*)\}", r"\1", body).strip()
            macros[m.group(1)] = body  # KaTeX 宏值支持 #1/#2 形参
        for m in re.finditer(
                r"\\DeclareMathOperator\s*\*?\s*\{(\\[A-Za-z]+)\}\s*\{", preamble):
            body, _ = _read_group(preamble, m.end() - 1)
            macros[m.group(1)] = "\\operatorname{" + body + "}"
    return macros


# ---------- 引用编号（按全文首次出现顺序） ----------

class _CiteIndex:
    def __init__(self) -> None:
        self.order: dict[str, int] = {}

    def numbers(self, keys: str) -> list[int]:
        out = []
        for k in keys.split(","):
            k = k.strip()
            if not k:
                continue
            if k not in self.order:
                self.order[k] = len(self.order) + 1
            out.append(self.order[k])
        return out


# ---------- 行内清理：命令 → 纯文本/标记 ----------

_CITE_RE = re.compile(
    r"\\(?:cite|citep|citet|citealp|citeauthor|citeyear|parencite|textcite)"
    r"\*?(?:\[[^\]]*\])*\{([^}]*)\}")
_REF_RE = re.compile(r"\\(?:eqref|autoref|cref|Cref|ref|pageref)\{([^}]*)\}")
# 单参数"样式"命令：只保留内容
_STYLE_CMDS = ("emph", "textbf", "textit", "textsl", "textsc", "textrm", "textsf",
               "texttt", "text", "underline", "mbox", "makebox", "noindent",
               "paragraph", "subparagraph")


# 需要连同参数整体丢弃的版式命令（页眉页脚、目录、文献样式等）
_DROP_GROUP_RE = re.compile(
    r"\\(?:lhead|chead|rhead|lfoot|cfoot|rfoot|fancyhead|fancyfoot|"
    r"pagestyle|thispagestyle|setcounter|addcontentsline|bibliographystyle|"
    r"bibliography|input|include|usepackage|RequirePackage|documentclass|"
    r"vspace\*?|hspace\*?|thanks|footnotemark|label|index)"
    r"(?:\[[^\]]*\])?\s*\{[^{}]*\}")
_DROP_TOKEN_RE = re.compile(
    r"\\(?:maketitle|tableofcontents|listoffigures|listoftables|clearpage|"
    r"cleardoublepage|newpage|bigskip|medskip|smallskip|noindent)\b")
# 数学段先保护再恢复，清理动作绝不动 $...$ 内部
_MATH_RE = re.compile(
    r"(\$\$.*?\$\$|\\\(.+?\\\)|\\\[.+?\\\]|\$(?:\\.|[^$\\])+\$)", re.S)


def _clean_inline(text: str, cites: _CiteIndex) -> str:
    maths: list[str] = []

    def stash(m: re.Match) -> str:
        maths.append(m.group(1))
        return f"\x00{len(maths) - 1}\x00"

    text = _MATH_RE.sub(stash, text)
    # 先丢无参版式命令（\noindent 等）：否则样式命令展开后会和它粘成
    # \noindentMove 这种"长命令名"，被未知命令规则整段吞掉
    text = _DROP_TOKEN_RE.sub("", text)

    def cite_sub(m: re.Match) -> str:
        nums = cites.numbers(m.group(1))
        return "⟦" + ",".join(str(n) for n in nums) + "⟧" if nums else ""

    prev = None
    while prev != text:  # 迭代展开嵌套的样式命令
        prev = text
        text = _CITE_RE.sub(cite_sub, text)
        text = _REF_RE.sub(lambda m: f"⟦§:{m.group(1)}⟧", text)
        text = _DROP_GROUP_RE.sub("", text)
        for cmd in _STYLE_CMDS:
            # 替换后补空格：防止前方残留命令与内容粘连（如 \foo\textbf{X} →
            # \fooX），被后面的"未知命令"规则把 X 一并吞掉
            text = re.sub(r"\\" + cmd + r"\s*\{([^{}]*)\}", r"\1 ", text)
        text = re.sub(r"\\footnote\s*\{([^{}]*)\}", r"（注：\1）", text)
        text = re.sub(r"\\href\{[^{}]*\}\{([^{}]*)\}", r"\1", text)
        text = re.sub(r"\\url\{([^{}]*)\}", r"\1", text)
    text = text.replace("\\\\", " ")
    text = text.replace("~", " ").replace("``", "“").replace("''", "”")
    text = text.replace("---", "—").replace("--", "–")
    text = re.sub(r"\\([&%$#_{}])", r"\1", text)
    text = re.sub(r"\\[,;:!]", " ", text)
    # 残留的未知无参命令直接丢弃（如 \today）
    text = re.sub(r"\\[a-zA-Z]+\*?(?![a-zA-Z])", "", text)
    # 数学已保护，剩下的裸花括号都是分组壳，剥掉
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    # 恢复数学段
    text = re.sub(r"\x00(\d+)\x00", lambda m: maths[int(m.group(1))], text)
    return text.strip()


# ---------- 结构抽取 ----------

_ENV_MATH = ("equation", "align", "gather", "multline", "eqnarray", "displaymath")
_ENV_FIGTAB = ("figure", "table", "wrapfigure")
_ENV_LIST = ("itemize", "enumerate", "description")
# 算法/代码环境：内容按代码块保留，不跑正文清理
_ENV_CODE = ("algorithmic", "lstlisting", "verbatim")
_SECTION_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection)\*?\{")
_CAPTION_RE = re.compile(r"\\caption(?:\[[^\]]*\])?\s*\{")

_ENV_NAMES = _ENV_MATH + _ENV_FIGTAB + _ENV_LIST + _ENV_CODE + (
    "algorithm", "abstract", "quote", "quotation", "center", "flushleft",
    "minipage")
_ENV_RE = re.compile(r"\\begin\{((?:" + "|".join(_ENV_NAMES) + r")\*?)\}")

_IMG_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
_IMG_EXTS = (".pdf", ".png", ".jpg", ".jpeg")


def _resolve_images(body: str, base_dir: Path, root: Path) -> list[str]:
    """figure 环境里的 \\includegraphics 路径 → 相对源码根目录的实际文件路径。

    路径可能不带扩展名（LaTeX 惯例），依次尝试常见位图/矢量格式；
    先按当前 tex 文件所在目录解析，再按源码根目录解析。
    """
    out: list[str] = []
    for m in _IMG_RE.finditer(body):
        rel = m.group(1).strip()
        # 去掉路径里的宏（如 \paperroot/figures/x.pdf），宏通常是路径前缀变量
        rel = re.sub(r"\\[A-Za-z]+\b", "", rel).lstrip("/")
        if not rel:
            continue
        for base in (base_dir, root):
            cand = base / rel
            found: Path | None = None
            if cand.suffix:
                if cand.is_file():
                    found = cand
            else:
                for ext in _IMG_EXTS:
                    if cand.with_suffix(ext).is_file():
                        found = cand.with_suffix(ext)
                        break
            if found is not None:
                out.append(found.relative_to(root).as_posix())
                break
    return out


def _find_env_spans(text: str) -> list[tuple[int, int, str, str]]:
    """返回 [(start, end, env_name, env_body)]，按出现顺序。不处理同名嵌套。"""
    spans = []
    for m in _ENV_RE.finditer(text):
        name = m.group(1)
        end_m = re.search(r"\\end\{" + re.escape(name) + r"\}", text[m.end():])
        if not end_m:
            continue
        body_end = m.end() + end_m.start()
        body = text[m.end():body_end]
        # 剥掉环境开头的可选参数：所有环境先剥 [..]；minipage 再剥 {宽度}
        body = re.sub(r"^\s*\[[^\]]*\]", "", body)
        if name.rstrip("*") == "minipage":
            body = re.sub(r"^\s*\{[^{}]*\}", "", body)
        spans.append((m.start(), m.end() + end_m.end(), name, body))
    spans.sort()
    return spans


def _split_plain(segment: str, cites: _CiteIndex) -> list[dict]:
    """非环境文本 → 标题段 + 空行分隔的正文段。"""
    out: list[dict] = []
    pos = 0
    for m in _SECTION_RE.finditer(segment):
        out.extend(_paragraphs(segment[pos:m.start()], cites))
        title, _ = _read_group(segment, m.end() - 1)
        level = {"part": 1, "chapter": 1, "section": 2, "subsection": 3,
                 "subsubsection": 4}[m.group(1)]
        out.append({"kind": "heading", "level": level,
                    "text": _clean_inline(title, cites)})
        pos = m.end() - 1 + len(title) + 2
    out.extend(_paragraphs(segment[pos:], cites))
    return out


def _paragraphs(text: str, cites: _CiteIndex) -> list[dict]:
    out = []
    for chunk in re.split(r"\n\s*\n", text):
        cleaned = _clean_inline(chunk, cites)
        # 丢掉只剩命令骨架的段（如 \maketitle 行）
        if len(re.sub(r"⟦[^⟧]*⟧", "", cleaned)) >= 2:
            out.append({"kind": "para", "level": 0, "text": cleaned})
    return out


_ALGO_CMDS: list[tuple[str, str]] = [
    (r"\\Procedure\{([^{}]*)\}\{([^{}]*)\}", r"\nprocedure \1(\2)"),
    (r"\\Function\{([^{}]*)\}\{([^{}]*)\}", r"\nfunction \1(\2)"),
    (r"\\End(?:Procedure|Function)", r"\nend"),
    (r"\\If\{([^{}]*)\}", r"\nif \1"),
    (r"\\ElsIf\{([^{}]*)\}", r"\nelse if \1"),
    (r"\\Else\b", r"\nelse"),
    (r"\\For(?:All)?\{([^{}]*)\}", r"\nfor \1"),
    (r"\\While\{([^{}]*)\}", r"\nwhile \1"),
    (r"\\Loop\b", r"\nloop"),
    (r"\\Repeat\b", r"\nrepeat"),
    (r"\\Until\{([^{}]*)\}", r"\nuntil \1"),
    (r"\\End(?:If|For|While|Loop|Repeat)\b", r"\nend"),
    (r"\\Return\s*", r"\nreturn "),
    (r"\\Statex?\s*", "\n"),
    (r"\\Require\s*", r"\nInput: "),
    (r"\\Ensure\s*", r"\nOutput: "),
    (r"\\Comment\{([^{}]*)\}", r"  # \1"),
    (r"\\Call\{([^{}]*)\}\{([^{}]*)\}", r"\1(\2)"),
    (r"\\Comment\b", "  # "),
]


def _algo_to_code(body: str, cites: _CiteIndex) -> str:
    """algorithmic/lstlisting 内容 → 保留换行的代码块文本（数学仍走占位保护）。"""
    maths: list[str] = []

    def stash(m: re.Match) -> str:
        maths.append(m.group(1))
        return f"\x00{len(maths) - 1}\x00"

    body = re.sub(r"^\s*\[[^\]]*\]", "", body)  # \begin{algorithmic}[1] 的行号参数
    body = _MATH_RE.sub(stash, body)
    body = _CITE_RE.sub(
        lambda m: "⟦" + ",".join(map(str, cites.numbers(m.group(1)))) + "⟧",
        body)
    for pat, rep in _ALGO_CMDS:
        body = re.sub(pat, rep, body)
    body = re.sub(r"\\(?:caption|label)\b.*", "", body)  # caption 由上层提取
    body = re.sub(r"\\[a-zA-Z]+\*?(?![a-zA-Z])", "", body)
    body = re.sub(r"[{}]", "", body)
    body = re.sub(r"[^\S\n]+", " ", body)          # 折叠空格但保留换行
    body = re.sub(r"\n\s*\n+", "\n", body)
    body = re.sub(r"\x00(\d+)\x00",
                  lambda m: maths[int(m.group(1))], body)
    return body.strip()


def _env_blocks(name: str, body: str, cites: _CiteIndex,
                base_dir: Path, root: Path) -> list[dict]:
    base = name.rstrip("*")
    if base in _ENV_MATH:
        inner = body.strip()
        if base in ("align", "gather", "multline", "eqnarray"):
            inner = "\\begin{aligned}" + inner + "\\end{aligned}"
        inner = re.sub(r"\\(?:label|tag)\{[^{}]*\}", "", inner)
        return [{"kind": "math", "level": 0, "text": "$$" + inner + "$$",
                 "images": []}]
    if base in _ENV_FIGTAB:
        # 主图注：先剥掉 subfigure/subtable 子环境，避免把子图注当成主图注
        main_body = re.sub(
            r"\\begin\{(?:subfigure|subtable)\*?\}.*?\\end\{(?:subfigure|subtable)\*?\}",
            "", body, flags=re.S)
        m = _CAPTION_RE.search(main_body) or _CAPTION_RE.search(body)
        cap = ""
        if m:
            raw, _ = _read_group(body if m.string is body else main_body,
                                 m.end() - 1)
            cap = _clean_inline(raw, cites)
        images = _resolve_images(body, base_dir, root)
        if not cap and not images:
            return []
        return [{"kind": "figure" if images else "caption", "level": 0,
                 "text": cap, "images": images}]
    if base in _ENV_LIST:
        out = []
        for part in re.split(r"\\item(?:\[[^\]]*\])?", body):
            cleaned = _clean_inline(part, cites)
            if len(cleaned) >= 2:
                out.append({"kind": "item", "level": 0,
                            "text": "• " + cleaned, "images": []})
        return out
    if base in _ENV_CODE:
        code = _algo_to_code(body, cites)
        return [{"kind": "code", "level": 0, "text": code, "images": []}] \
            if code else []
    if base == "algorithm":
        # 算法浮动体：caption + 内部 algorithmic 代码块
        out: list[dict] = []
        m = _CAPTION_RE.search(body)
        if m:
            raw, _ = _read_group(body, m.end() - 1)
            cap = _clean_inline(raw, cites)
            if cap:
                out.append({"kind": "caption", "level": 0, "text": cap,
                            "images": []})
        for _s, _e, inner_name, inner_body in _find_env_spans(body):
            if inner_name.rstrip("*") in _ENV_CODE:
                code = _algo_to_code(inner_body, cites)
                if code:
                    out.append({"kind": "code", "level": 0, "text": code,
                                "images": []})
        return out
    # abstract / quote / center 等透明环境：正文继续按段抽
    return _split_plain(body, cites)


def _front_matter(text: str, cites: _CiteIndex) -> list[dict]:
    """导言区的 \\title / \\author → 文首的标题段与作者段。"""
    out: list[dict] = []
    end = text.find("\\begin{document}")
    pre = _strip_comments(text[:end]) if end >= 0 else ""
    m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{", pre)
    if m:
        t, _ = _read_group(pre, m.end() - 1)
        t = _clean_inline(t, cites)
        if t:
            out.append({"kind": "heading", "level": 1, "text": t})
    m = re.search(r"\\author\s*\{", pre)
    if m:
        a, _ = _read_group(pre, m.end() - 1)
        a = re.sub(r"\\(?:and|AND)\b", " · ", a)
        a = _clean_inline(a, cites)
        if a:
            out.append({"kind": "para", "level": 0, "text": a})
    return out


def _doc_files(main: Path, texs: list[Path]) -> list[Path]:
    """按文档顺序解析 \\input/\\include 链。

    与引擎的 collect_doc_files 不同：跳过 .bbl 引用（\\bibliography 被改写为
    \\input{x.bbl} 后，按 stem 解析会错配到同名 .tex 文件）；
    主文件只跟随正文区的引用（导言区的样式文件不产生段落）。
    """
    by_stem: dict[str, Path] = {}
    for p in texs:
        by_stem.setdefault(p.stem, p)
        by_stem.setdefault(p.name, p)
    order: list[Path] = []
    seen: set[Path] = set()

    def walk(path: Path, body_only: bool) -> None:
        if path in seen:
            return
        seen.add(path)
        order.append(path)
        try:
            text = textio.load_text(path)
        except OSError:
            return
        region = _body(text) if body_only else text
        for m in re.finditer(r"\\(?:input|include)\{([^{}]+)\}", region):
            name = m.group(1).strip()
            if name.endswith(".bbl"):
                continue
            target = by_stem.get(name) or by_stem.get(Path(name).stem)
            if target is not None:
                walk(target, False)

    walk(main, True)
    return order


def extract_tex_paragraphs(root: Path, cites: _CiteIndex) -> list[dict]:
    """按文档顺序抽取一个源码树的全部段落。"""
    texs = sorted(root.rglob("*.tex"))
    if not texs:
        return []
    try:
        main = find_main_tex(texs)
        files = _doc_files(main, texs)
    except Exception:  # noqa: BLE001 - 主文件定位失败则退化为全量
        main = None
        files = texs
    out: list[dict] = []
    for path in files:
        try:
            text = _strip_comments(textio.load_text(path))
        except OSError:
            continue
        # \iffalse ... \fi 是被条件编译注释掉的整块内容，直接剥掉
        text = re.sub(r"\\iffalse\b.*?\\fi\b", "", text, flags=re.S)
        if path == main:
            out.extend(_front_matter(text, cites))
        # \input 进来的子文件没有 \begin{document}，全文即正文
        body = _body(text) if path == main else text
        if not body:
            continue
        pos = 0
        for start, end, name, env_body in _find_env_spans(body):
            if start < pos:
                continue  # 嵌套在已处理环境内部（如 algorithm 里的 algorithmic）
            out.extend(_split_plain(body[pos:start], cites))
            out.extend(_env_blocks(name, env_body, cites,
                                   base_dir=path.parent, root=root))
            pos = end
        out.extend(_split_plain(body[pos:], cites))
    return out


def _signature(p: dict, occ: int) -> tuple:
    """段落对齐签名：只含不随翻译变化的成分。

    插图按图片路径精确锚定；展示公式逐字相同；标题按层级；正文段取
    引用编号 ⟦n⟧、交叉引用 ⟦§:label⟧、行内公式、数字这些不变 token。
    完全没有不变 token 的纯文字段用出现序号当键——绝不互相锚定，
    只在 replace 区间里按相对位置配对。
    """
    kind = p["kind"]
    if kind == "figure":
        return ("fig", tuple(p.get("images") or []))
    if kind == "math":
        return ("math", re.sub(r"\s+", "", p["text"]))
    if kind == "heading":
        return ("h", p["level"], p["text"][:24])
    toks = re.findall(r"⟦[^⟧]*⟧", p["text"])
    toks += re.findall(r"\$(?:\\.|[^$\\])+\$", p["text"])
    toks += re.findall(r"\d+(?:\.\d+)?", p["text"])
    if toks:
        return ("t", tuple(toks))
    return ("raw", occ)


def _align_seq(src: list[dict], tgt: list[dict]) -> list[tuple[dict, dict]]:
    """difflib 序列对齐：不变 token 当锚点，区间内部按相对位置配对。

    翻译可能在段内插入空行（一段变两段）或合并段落，所以不能按序号硬配；
    纯比例对齐又会随长度差整体漂移。锚点（图片路径/展示公式/引用编号/
    行内公式/数字）在两种语言里逐字相同，先把两边锁在一起，区间内部
    再局部配对：多余的 tgt 段合并进同一 src，未配上的 src 保留原文。
    """
    if not src or not tgt:
        return [(s, {"kind": s["kind"], "level": s["level"], "text": "",
                     "images": []}) for s in src]

    s_keys = [_signature(p, i) for i, p in enumerate(src)]
    t_keys = [_signature(p, j) for j, p in enumerate(tgt)]
    sm = difflib.SequenceMatcher(None, s_keys, t_keys, autojunk=False)

    pairs: list[tuple[dict, dict]] = []

    def merge_tgt(items: list[dict]) -> dict:
        if len(items) == 1:
            return items[0]
        images: list[str] = []
        for it in items:
            images.extend(it.get("images") or [])
        return {"kind": items[0]["kind"], "level": items[0]["level"],
                "text": " ".join(it["text"] for it in items if it["text"]),
                "images": images}

    def pair_region(ss: list[dict], ts: list[dict]) -> None:
        """一个 replace 区间：局部按相对位置配对。"""
        if not ts:
            for s in ss:
                pairs.append((s, {"kind": s["kind"], "level": s["level"],
                                  "text": "", "images": []}))
            return
        if not ss:
            # 多出来的译文段：并入前一对的译文，不丢内容
            if pairs:
                s0, t0 = pairs[-1]
                pairs[-1] = (s0, merge_tgt([t0, *ts]))
            return
        ns, nt = len(ss), len(ts)
        grouped: dict[int, list[dict]] = {}
        i = 0
        for j, t in enumerate(ts):
            rt = j / nt
            while i + 1 < ns \
                    and abs((i + 1) / ns - rt) < abs(i / ns - rt) - 1e-12:
                i += 1
            grouped.setdefault(i, []).append(t)
        for si, s in enumerate(ss):
            items = grouped.get(si)
            if items:
                pairs.append((s, merge_tgt(items)))
            else:
                pairs.append((s, {"kind": s["kind"], "level": s["level"],
                                  "text": "", "images": []}))

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pairs.append((src[i1 + k], tgt[j1 + k]))
        else:
            pair_region(src[i1:i2], tgt[j1:j2])
    return pairs


def tex_pairs(task_dir: str | Path) -> dict | None:
    """arXiv 任务的源码级段落对；任务不含 arXiv 源码时返回 None。"""
    task_dir = Path(task_dir)
    src_root, out_root = task_dir / "arxiv_src", task_dir / "arxiv_out"
    if not src_root.is_dir() or not out_root.is_dir():
        return None
    src_paras = extract_tex_paragraphs(src_root, _CiteIndex())
    tgt_paras = extract_tex_paragraphs(out_root, _CiteIndex())
    if not src_paras or not tgt_paras:
        return None
    macros = collect_macros(
        sorted(out_root.rglob("*.tex")) + sorted(src_root.rglob("*.tex")))
    pairs = [
        {"i": i, "kind": s["kind"], "level": s["level"],
         "source": s["text"], "target": t["text"],
         "images": t.get("images") or s.get("images") or []}
        for i, (s, t) in enumerate(_align_seq(src_paras, tgt_paras))
        if s["text"] or t["text"] or s.get("images")
    ]
    return {"ready": True, "mode": "tex", "macros": macros, "pairs": pairs,
            "source_count": len(src_paras), "target_count": len(tgt_paras)}
