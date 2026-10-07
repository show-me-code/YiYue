"""回归测试：针对代码审查确认并已修复的缺陷。

每个用例都对着一个**实测过的**失灵现象，改坏就会红。
"""

import io
import tarfile
from pathlib import Path

import pytest

from app.services import arxiv_engine, cache, tex_translate, textio, verify


# ---------- tex_translate：\title 与图注的 [[C#]] 编号冲突 ----------

class _EchoTranslator:
    """把每段前加"译|"，便于断言"这段确实走了翻译"。"""

    def translate(self, text, ignore_cache=False):
        return "译|" + text.replace("\n", " ")


class _MarkerTranslator:
    def translate(self, text, ignore_cache=False):
        return "译"


def test_title_not_clobbered_by_first_caption():
    r"""回归：导言区 \title 与正文第一个 \caption 都从 [[C0]] 起编号。

    修复前 \title{My Real Title} 会被替换成第一个图注内容
    （实测输出 `\title{译|First caption}`，标题被静默吃掉）。
    """
    tex = ("\\documentclass{article}\n\\title{My Real Title}\n"
           "\\begin{document}\n\\caption{First caption}\n"
           "\\section{Intro}\nbody\n\\end{document}\n")
    out = tex_translate.translate_tex(tex, _EchoTranslator(), "zh")
    title_line = next(ln for ln in out.splitlines() if ln.startswith("\\title"))
    assert "My Real Title" in title_line
    assert "First caption" not in title_line
    assert "First caption" in out  # 图注自己也要被翻译，不能互相顶掉


def test_english_title_edits_apply_by_position():
    r"""回归：edits 必须按起始位置倒序应用，reversed(edits) 不等于位置倒序。

    子图注在文本里位于两个 \section 之前，但它是最后才被收集的；
    按收集顺序倒着改会把前面记录的偏移量顶偏，替换到错误位置。
    """
    text = ("\\subfloat[Alpha beta gamma]{\\includegraphics{a}}\n"
            "\\section{Introduction}\n"
            "\\section{Related Work}\n")
    out = tex_translate._translate_english_titles(text, _MarkerTranslator(), None)
    assert out == ("\\subfloat[译]{\\includegraphics{a}}\n"
                   "\\section{译}\n\\section{译}\n")


def test_traditional_chinese_gets_ctex_and_traditional_labels():
    """回归：lang_out='zt' 之前完全不注入 ctex（只认 zh 前缀），也没有繁体标签。"""
    tex = "\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n"
    zt = tex_translate.prepare_for_xetex(tex, "zt", 1.0)
    assert "fontset=fandol" in zt and "ctex" in zt
    assert "\\renewcommand\\figurename{圖}" in zt
    assert "參考文獻" in zt and "附錄" in zt

    zh = tex_translate.prepare_for_xetex(tex, "zh", 1.0)
    assert "\\renewcommand\\figurename{图}" in zh
    assert "参考文献" in zh

    # 非中文目标语言不能注入 ctex
    assert "ctex" not in tex_translate.prepare_for_xetex(tex, "en", 1.0)


@pytest.mark.parametrize("lang", ["zh-CN", "zh_CN", "zh", "zt", "ZT"])
def test_needs_ctex_lang_families(lang):
    assert tex_translate.needs_ctex(lang)


@pytest.mark.parametrize("lang", ["en", "ja", "ko", ""])
def test_needs_ctex_negative(lang):
    assert not tex_translate.needs_ctex(lang)


def test_restore_does_not_expand_caption_tokens():
    r"""回归：_restore 只能展开 M 命名空间。

    protected 是 M 列表，用它去查 [[C#]] 会命中完全无关的受保护片段。迭代展开
    把这个问题从"恰好没触发"变成必然：实测 `\caption{[[C7]]}` 被换成一整段
    `\begin{equation*}…\end{figure}`，编译报
    `File ended while scanning use of \caption@ydblarg`，整篇回退原位排版。
    """
    protected = ["\\begin{equation*}x\\end{equation*}"]
    text = "\\caption{[[C0]]}"
    assert tex_translate._restore(text, protected) == text
    assert tex_translate._restore("[[M0]]", protected) == protected[0]
    assert tex_translate._restore("[[ M0 ]]", protected) == protected[0]


def test_caption_inside_figure_env_survives_translation():
    """图注占位符位于受保护的 figure 环境内部：M 展开后 C 必须还在，交给 put_caps。"""
    tex = ("\\documentclass{article}\n\\begin{document}\n"
           "\\begin{figure}\n\\includegraphics{a}\n"
           "\\caption{Hello caption}\n\\label{fig:a}\n\\end{figure}\n"
           "\\section{Intro}\nbody text\n\\end{document}\n")
    out = tex_translate.translate_tex(tex, _EchoTranslator(), "zh")
    assert (out.count("{"), out.count("}")) == (tex.count("{"), tex.count("}"))
    assert "\\caption{译|Hello caption}" in out
    assert "[[" not in out          # 占位符一个都不能漏到成品里
    assert "\\includegraphics{a}" in out and "\\label{fig:a}" in out


def test_restore_expands_nested_placeholders():
    r"""回归：注释行整行受保护时会把已保护的 `\citep{key}` 再包一层，
    单遍 _restore 只能拆开外层，内层 [[M#]] 原样留在成品源码里
    （实测 appendix.tex 等文件里有几十处）。"""
    tex = "text % \\citep{key} 注释\n"
    protected_text, frags = tex_translate._build_protection(tex)
    assert len(frags) >= 2
    restored = tex_translate._restore(protected_text, frags)
    assert restored == tex
    assert "[[" not in restored


def test_restore_does_not_eat_braces():
    r"""回归：\hbox{[[M0]]} 的花括号必须原样保留。"""
    protected = ["$x$"]
    assert tex_translate._restore("\\hbox{[[M0]]}", protected) == "\\hbox{$x$}"


def test_starred_cite_fully_protected():
    r"""回归：`\b` 在 `\citep*` 的 `*` 后不成立，正则退回只匹配 `\citep`，
    后面的 {key} 留在正文里被当散文翻译（引用键被改坏）。"""
    protected_text, frags = tex_translate._build_protection(
        "see \\citep*{smith2017} and \\citet{jon01}.")
    assert frags == ["\\citep*{smith2017}", "\\citet{jon01}"]
    assert "{smith2017}" not in protected_text
    assert protected_text.strip() == "see [[M0]] and [[M1]]."


# ---------- textio：坏字节不能再被固化成 U+FFFD ----------

def test_lenient_read_does_not_mangle_latin1():
    raw = b"caf\xe9 na\xefve \x93quote\x94"
    text, enc = textio.decode_bytes(raw)
    assert "\ufffd" not in text
    assert "café" in text and "naïve" in text
    assert enc == "cp1252"


def test_lenient_read_falls_back_to_latin1_for_undefined_cp1252_byte():
    raw = b"value\x81end"  # 0x81 在 cp1252 里未定义，latin-1 必须兜住
    text, enc = textio.decode_bytes(raw)
    assert "\ufffd" not in text and enc == "latin-1"


def test_lenient_read_prefers_utf8():
    text, enc = textio.decode_bytes("中文 café".encode("utf-8"))
    assert enc == "utf-8" and text == "中文 café"


def test_load_text_normalizes_crlf(tmp_path):
    """回归：read_bytes() 不做通用换行，CRLF 源码写回会变成 \\r\\r\\n。

    TeX 把 CR 和 LF 都当行尾，\\r\\r\\n 就成了空行 = 段落结束——整篇源码每行都
    断成一段，实测报 `! Paragraph ended before \\author was complete`，
    arXiv 引擎全线回退到 BPDF 排版。
    """
    src = tmp_path / "main.tex"
    src.write_bytes(b"\\documentclass{article}\r\n\\author{A}\r\n\r\n"
                    b"\\begin{document}\r\nx\r\n\\end{document}\r\n")
    text = textio.load_text(src)
    assert "\r" not in text

    textio.save_text(src, text)
    raw = src.read_bytes()
    assert b"\r\r" not in raw
    assert b"\\author{A}\r\n" in raw          # 仍是可用的 CRLF，但没有双 CR


def test_crlf_source_survives_prepare_for_xetex(tmp_path):
    src = tmp_path / "main.tex"
    src.write_bytes(("\\documentclass{article}\r\n\\author{A}\r\n"
                     "\\begin{document}\r\nx\r\n\\end{document}\r\n").encode("utf-8"))
    out = tex_translate.prepare_for_xetex(textio.load_text(src), "zh", 1.3)
    textio.save_text(src, out)
    raw = src.read_bytes()
    # 写回后是单 CR 的 CRLF（TeX 正常接受）；双 CR 才是空行=段落结束的元凶
    assert b"\r\r" not in raw
    assert b"fontset=fandol" in raw
    assert raw.count(b"\\r\\n") == 0


def test_crlf_source_survives_use_shipped_bbl(tmp_path):
    """use_shipped_bbl 用 splitlines(keepends=True) 重组文件，同样不能留下双 CR。"""
    root = tmp_path / "src"
    root.mkdir()
    (root / "main.tex").write_bytes(
        b"\\documentclass{article}\r\n\\bibliography{refs}\r\n"
        b"\\begin{document}\r\nx\r\n\\end{document}\r\n")
    (root / "refs.bbl").write_bytes(b"\\begin{thebibliography}{1}\r\n\\end{thebibliography}\r\n")
    arxiv_engine.use_shipped_bbl(root, root / "main.tex")
    raw = (root / "main.tex").read_bytes()
    assert b"\r\r" not in raw
    assert b"\\input{refs.bbl}\r\n" in raw


def test_gz_source_gets_newlines_normalized(tmp_path):
    """单文件 gz 的 tex 也要统一换行（旧代码在这里同样会写出 \\r\\r\\n）。"""
    import gzip

    raw = gzip.compress(b"\\documentclass{article}\r\n\\author{A}\r\n"
                        b"\\begin{document}\r\nx\r\n\\end{document}\r\n")
    src = tmp_path / "source.bin"
    src.write_bytes(raw)
    dest = tmp_path / "out"
    texs = arxiv_engine.extract_source(src, dest)
    assert len(texs) == 1
    raw = texs[0].read_bytes()
    assert b"\r\r" not in raw            # 不能出现双 CR（= TeX 的空行）
    assert b"\\author{A}" in raw


def test_patch_style_files_keeps_latin1_chars(tmp_path):
    sty = tmp_path / "mystyle.sty"
    sty.write_bytes(b"\\def\\abstractname{caf\xe9 {\\bf Abstract}}\n")
    assert tex_translate.patch_style_files(tmp_path) == 1
    out = sty.read_text("utf-8")
    assert "摘要" in out and "café" in out and "\ufffd" not in out


def test_patch_style_files_handles_newline_after_font_switch(tmp_path):
    r"""回归：iclr2026_conference.sty 把 "Abstract" 写在 {\large\sc 的**下一行**，
    原有三条模式都要求字体命令与 Abstract 之间没有换行，于是成品页面上一直留着
    英文 "Abstract"（实测 2607.03728 第 1 页）。"""
    sty = tmp_path / "iclr2026_conference.sty"
    sty.write_text(
        "\\renewenvironment{abstract}{\\vskip.075in\\centerline{\\large\\sc\n"
        "Abstract}\\vspace{0.5ex}\\begin{quote}}{\\par\\end{quote}}\n"
        "\\def\\abstractname{Abstract}\n",
        "utf-8")
    assert tex_translate.patch_style_files(tmp_path) == 1
    out = sty.read_text("utf-8")
    assert "Abstract" not in out
    assert "摘要}\n" in out.replace("\r\n", "\n")
    assert "\\def\\abstractname{摘要}" in out


# ---------- arxiv_engine：解包安全 ----------

def _tar_with(names: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in names.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        for name, link in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = link
            t.addfile(info)
    return buf.getvalue()


@pytest.mark.parametrize("evil", [
    "../escape.tex", "/abs/escape.tex", "C:/evil.tex", "..\\escape.tex",
    "a/../../escape.tex",
])
def test_extract_source_rejects_traversal(tmp_path, evil):
    raw = _tar_with({"main.tex": b"\\documentclass{article}", evil: b"pwned"})
    src = tmp_path / "source.bin"
    src.write_bytes(raw)
    dest = tmp_path / "out"
    texs = arxiv_engine.extract_source(src, dest)
    assert [p.name for p in texs] == ["main.tex"]
    assert not (tmp_path / "escape.tex").exists()
    assert not Path("C:/evil.tex").exists()


def test_extract_source_rejects_symlink(tmp_path):
    raw = _tar_with({"main.tex": b"\\documentclass{article}"},
                    symlinks={"link.tex": "../../outside.tex"})
    src = tmp_path / "source.bin"
    src.write_bytes(raw)
    dest = tmp_path / "out"
    arxiv_engine.extract_source(src, dest)
    assert not (dest / "link.tex").exists()
    assert not (dest / "link.tex").is_symlink()


# ---------- arxiv_engine：注释里的 \bibliography 不能被当成真的 ----------

def _prep_bbl_dir(tmp_path, main_tex_body: str) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    (root / "main.tex").write_text(main_tex_body, "utf-8")
    (root / "main.bbl").write_text("\\begin{thebibliography}{1}\n\\end{thebibliography}\n",
                                  "utf-8")
    return root


def test_use_shipped_bbl_ignores_commented_bibliography(tmp_path):
    r"""回归：注释行里的 `% \bibliography{old}` 若出现在真命令之前，
    "只替换第一处"会把注释改掉、真命令原样留下，引用照样全变 (?)。"""
    root = _prep_bbl_dir(
        tmp_path,
        "\\documentclass{article}\n"
        "% \\bibliography{old}\n"
        "\\bibliography{alain17a}\n"
        "\\begin{document}x\\end{document}\n")
    info = arxiv_engine.use_shipped_bbl(root, root / "main.tex")
    text = (root / "main.tex").read_text("utf-8")
    assert info["applied"] is True
    assert "% \\bibliography{old}" in text          # 注释原样
    assert "\\input{alain17a.bbl}" in text          # 真命令被改写
    assert (root / "alain17a.bbl").exists()


def test_use_shipped_bbl_handles_inline_comment_and_multiple_names(tmp_path):
    root = _prep_bbl_dir(
        tmp_path,
        "\\documentclass{article}\n"
        "\\bibliography{a,b} % 两个文献库\n"
        "\\begin{document}x\\end{document}\n")
    info = arxiv_engine.use_shipped_bbl(root, root / "main.tex")
    text = (root / "main.tex").read_text("utf-8")
    assert info["applied"] is True
    assert "\\input{a.bbl}\\input{b.bbl}" in text
    assert "% 两个文献库" in text


def test_use_shipped_bbl_keeps_subdir_in_input_path(tmp_path):
    r"""回归：\bibliography{bib/refs} 的 .bbl 落在 bib/ 下，
    \input 路径必须跟着走，否则引用照样全变 (?)。"""
    root = _prep_bbl_dir(
        tmp_path,
        "\\documentclass{article}\n"
        "\\bibliography{bib/refs}\n"
        "\\begin{document}x\\end{document}\n")
    info = arxiv_engine.use_shipped_bbl(root, root / "main.tex")
    assert info["applied"] is True
    assert "\\input{bib/refs.bbl}" in (root / "main.tex").read_text("utf-8")
    assert (root / "bib" / "refs.bbl").exists()


def test_use_shipped_bbl_noop_when_only_commented(tmp_path):
    root = _prep_bbl_dir(
        tmp_path,
        "\\documentclass{article}\n% \\bibliography{old}\n"
        "\\begin{document}x\\end{document}\n")
    info = arxiv_engine.use_shipped_bbl(root, root / "main.tex")
    assert info["applied"] is False
    assert "% \\bibliography{old}" in (root / "main.tex").read_text("utf-8")


# ---------- cache：登记失败不能把成功任务判成失败 ----------

def test_register_cache_failure_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "INDEX_PATH", tmp_path / "index.json")

    def boom(_index):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "_save_index", boom)
    assert cache.register_cache("k", 1) is False  # 不抛异常


def test_concurrent_register_keeps_all_keys(tmp_path, monkeypatch):
    """读-改-写没有锁时会丢更新，这里用多线程压一下。"""
    import threading

    monkeypatch.setattr(cache, "INDEX_PATH", tmp_path / "index.json")
    threads = [
        threading.Thread(target=cache.register_cache, args=(f"key{i}", i))
        for i in range(24)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    index = cache._load_index()
    assert len(index) == 24


# ---------- verify：超版心按页集合取差 ----------

def test_new_overflow_uses_page_set_difference():
    # 长度相同但页码不同：旧的 len() 比较会漏报
    assert verify.new_overflow_pages([2, 5], [1, 3]) == [2, 5]
    assert verify.new_overflow_pages([1, 2], [1, 2]) == []
    assert verify.new_overflow_pages([1, 3], [1]) == [3]
    assert verify.new_overflow_pages([3, 1], []) == [3, 1]


# ---------- orchestrator/pdfops：空页码范围必须失败 ----------

def test_extract_pages_empty_range_raises(tmp_path):
    import pymupdf

    from app.services.orchestrator import TaskManager

    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page()
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    doc.save(src)
    doc.close()

    with pytest.raises(ValueError, match="没有匹配到任何页"):
        TaskManager._extract_pages(src, dst, "9-12")
    assert not dst.exists()


def test_merge_pdfs_retries_replace(tmp_path, monkeypatch):
    """Windows 上目标文件被阅读器占用时替换会 PermissionError，必须重试。"""
    import pymupdf
    from app.services import pdfops

    doc = pymupdf.open()
    doc.new_page()
    part = tmp_path / "part.pdf"
    doc.save(part)
    doc.close()

    real_replace = Path.replace
    calls = {"n": 0}

    def flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "being used by another process")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(pdfops.time, "sleep", lambda _s: None)
    out = tmp_path / "mono.pdf"
    pdfops.merge_pdfs([part], out)
    assert out.exists() and calls["n"] == 2


# ---------- 编译修复：xcolor 选项冲突 / subfigure 垫片 ----------

def test_repair_xcolor_option_clash(tmp_path):
    r"""回归：论文 2607.03728 的源码**本身**就无法编译（Tectonic 内置 LaTeX 2021
    里 eso-pic 会先无选项加载 xcolor，之后 iclr_palette.tex 又带选项加载），
    报 `Option clash for package xcolor`，整条 arXiv 路线回退原位排版。
    修复：把作者自己写的选项用 \PassOptionsToPackage 提前到最早的加载点。"""
    (tmp_path / "iclr_palette.tex").write_text(
        "\\usepackage[table,dvipsnames]{xcolor}\n"
        "\\definecolor{INK}{HTML}{1B2A32}\n", "utf-8")
    tex = ("\\documentclass{article}\n"
           "\\usepackage{iclr2026_conference}\n"
           "\\input{iclr_palette}\n"
           "\\begin{document}x\\end{document}\n")
    log = ("error: iclr_palette:5: LaTeX Error: Option clash for package xcolor.\n"
           "! LaTeX Error: Option clash for package xcolor.\n")
    fixed = tex_translate.repair_from_log(tex, log, src_root=tmp_path)
    assert fixed is not None
    lines = fixed.splitlines()
    # 必须在 \documentclass 之前：\PassOptionsToPackage 只对之后的加载生效
    assert lines[0] == "\\PassOptionsToPackage{table,dvipsnames}{xcolor}"
    assert lines[1].startswith("\\documentclass{article}")
    # 幂等：已经修过就不再重复注入
    assert tex_translate.repair_from_log(fixed, log, src_root=tmp_path) is None
    # 找不到选项时不注入（宁可不改，也不要用错的选项集制造新冲突）
    assert tex_translate.repair_from_log(
        "\\documentclass{article}\n\\begin{document}x\\end{document}\n",
        log, src_root=None) is None


def test_repair_subfigure_shim_still_works():
    tex = "\\documentclass{article}\n\\begin{document}x\\end{document}\n"
    fixed = tex_translate.repair_from_log(
        tex, "! LaTeX Error: Environment subfigure undefined.")
    assert fixed is not None
    assert "newenvironment{subfigure}" in fixed
    assert fixed.index("\\documentclass") < fixed.index("newenvironment{subfigure}")


def test_repair_returns_none_for_unknown_error():
    tex = "\\documentclass{article}\n\\begin{document}x\\end{document}\n"
    assert tex_translate.repair_from_log(tex, "! Undefined control sequence.") is None


# ---------- 翻译卡死：单块墙钟上限 ----------

def test_chunk_timeout_reverts_without_hanging(monkeypatch):
    """回归：真跑 2607.03728 时整篇卡在某一块上 25 分钟不动
    （BabelDOC 对 RateLimitError 重试最多 100 次、退避最长 15s，HTTP 超时 600s/次）。
    单块必须有墙钟上限：到点保留原文继续，并计入统计。"""
    import time

    class Hanging:
        def translate(self, text, ignore_cache=False):
            time.sleep(3)           # 远超测试用的上限
            return text

    monkeypatch.setattr(tex_translate, "CHUNK_TIMEOUT_S", 0.3)
    stats: dict = {}
    start = time.monotonic()
    out = tex_translate._translate_chunk(Hanging(), "Some English sentence here.", stats)
    elapsed = time.monotonic() - start
    assert out is None                        # 保留原文
    assert elapsed < 2                        # 不会陪它等 3s
    assert stats["chunks_timeout"] == 1
    assert stats["chunks_untranslated"] == 1


def test_chunk_timeout_not_retried(monkeypatch):
    """超时不做 ignore_cache 重试：第一次就卡了，重试只会再等一个上限。"""
    import time

    class Hanging:
        def __init__(self):
            self.calls = 0

        def translate(self, text, ignore_cache=False):
            self.calls += 1
            time.sleep(3)
            return text

    monkeypatch.setattr(tex_translate, "CHUNK_TIMEOUT_S", 0.2)
    tr = Hanging()
    stats: dict = {}
    assert tex_translate._translate_chunk(tr, "Some English sentence here.", stats) is None
    assert tr.calls == 1


def test_call_translator_passes_through_normally():
    class Tr:
        def translate(self, text, ignore_cache=False):
            return ("retry:" if ignore_cache else "") + text.upper()

    assert tex_translate._call_translator(Tr(), "abc") == "ABC"
    assert tex_translate._call_translator(Tr(), "abc", ignore_cache=True) == "retry:ABC"


def test_translate_tex_survives_one_hanging_chunk(monkeypatch):
    """整篇不会因为一块卡死而永远不结束。"""
    import time

    class PartlyHanging:
        def translate(self, text, ignore_cache=False):
            if "SLOW" in text:
                time.sleep(3)
            return "译 " + text

    monkeypatch.setattr(tex_translate, "CHUNK_TIMEOUT_S", 0.3)
    tex = ("\\documentclass{article}\n\\begin{document}\n"
           "First paragraph with words.\n\nSLOW paragraph here.\n\n"
           "\\begin{figure}\\caption{cap}\\end{figure}\n"
           "\\end{document}\n")
    stats: dict = {}
    start = time.monotonic()
    out = tex_translate.translate_tex(tex, PartlyHanging(), "zh", None, stats)
    assert time.monotonic() - start < 10
    assert "译 " in out                       # 正常块被翻译
    assert "SLOW paragraph here." in out      # 卡死块保留原文
    assert stats.get("chunks_timeout", 0) >= 1


def test_apply_rate_limit_sets_babeldoc_limiter(monkeypatch):
    import babeldoc.translator.translator as bt

    calls: list[int] = []
    monkeypatch.setattr(bt, "set_translate_rate_limiter", calls.append)

    from app.services import babeldoc_runner

    class P:
        qps = 3

    babeldoc_runner.apply_rate_limit(P())
    assert calls == [3]

    class Zero:
        qps = 0

    babeldoc_runner.apply_rate_limit(Zero())
    assert calls == [3]        # 0/None 不动全局设置


def test_verify_reports_timeouts_end_to_end(tmp_path):
    """超时必须出现在验证报告的 reasons 里（不能静默）。"""
    import pymupdf

    from app.services import verify as v

    def make(path, text):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), text, fontsize=12)
        doc.save(path)
        doc.close()

    translated = tmp_path / "zh.pdf"
    baseline = tmp_path / "src.pdf"
    make(translated, "Translated body text. " * 20)
    make(baseline, "Source body text. " * 20)
    report = v.verify_translation(
        translated, baseline, tmp_path / "verify",
        issues={"chunks_timeout": 2, "captions_timeout": 1},
    )
    assert report["verdict"] == "需人工复核"
    assert any("3 块翻译超时未返回" in r for r in report["reasons"])
    assert (tmp_path / "verify" / "verify_report.json").exists()


# ---------- 实际引擎标注：不能挂上一轮的旧值 ----------

def test_retry_clears_stale_engine_used(client):
    """回归：试编译通过前引擎是上一轮回退留下的 babeldoc，重试后界面一直显示
    「原位排版（PDF 模式）」，让人误以为没走 LaTeX 源码（实测 2607.03728 就是
    这个观感：其实源码已下载并在用，只是第一次尝试因 xcolor 冲突回退过）。"""
    from sqlmodel import Session

    from app.db import engine as db_engine
    from app.models import Task

    with Session(db_engine) as s:
        t = Task(filename="arXiv-2607.03728.pdf", file_hash="h", size_bytes=1,
                 profile_id=1, status="cancelled",
                 engine_used="babeldoc", arxiv_id="2607.03728")
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

    resp = client.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["engine_used"] is None
    assert resp.json()["status"] == "queued"


def test_set_engine_used_updates_and_broadcasts(monkeypatch):
    m = _manager()
    seen: list[tuple] = []
    m._update_task = lambda tid, **kw: seen.append(("update", tid, kw)) or object()
    m._snapshot = lambda task: {"id": 1, "engine_used": "arxiv"}
    m.broadcast = lambda tid, payload: seen.append(("broadcast", tid, payload))
    m._set_engine_used(1, "arxiv")
    assert seen[0] == ("update", 1, {"engine_used": "arxiv"})
    assert seen[1][0] == "broadcast"
    assert seen[1][2]["type"] == "state"
    assert seen[1][2]["task"]["engine_used"] == "arxiv"


# ---------- arXiv 路径的提示词：自定义规范 + 术语表必须送到模型 ----------

def test_system_prompt_for_includes_placeholder_rules():
    default = tex_translate.system_prompt_for("zh")
    assert "Simplified Chinese" in default
    assert "[[M12]]" in default          # 占位符规则必须真的送给模型
    assert "Traditional Chinese" in tex_translate.system_prompt_for("zt")

    custom = tex_translate.system_prompt_for("zh", "学术翻译规范：信达雅")
    assert custom.startswith("学术翻译规范")
    assert "[[M12]]" in custom           # 自定义规范之后仍带上结构规则


def test_apply_arxiv_prompts_rewrites_system_and_user(monkeypatch):
    """回归：arXiv 路径用 do_translate 的写死提示词，配置档自定义规范、
    术语表、占位符规则全都不会到达模型（只在 BabelDOC 自己的流水线里生效）。"""
    from app.services import babeldoc_runner

    class FakeTranslator:
        def __init__(self):
            self.params: dict = {}

        def prompt(self, text):
            return [
                {"role": "system",
                 "content": "You are a professional,authentic machine translation engine."},
                {"role": "user", "content": f";; Input:\n\n{text}"},
            ]

        def add_cache_impact_parameters(self, k, v):
            self.params[k] = v

    class P:
        custom_system_prompt = "学术翻译规范：信、达、雅"

    tr = FakeTranslator()
    changed = babeldoc_runner.apply_arxiv_prompts(
        tr, P(), "zh", [{"source": "probe", "target": "探针"},
                        {"source": "layer", "target": "层"}])
    assert changed is True

    messages = tr.prompt("Hello [[M0]]")
    assert messages[0]["content"].startswith("学术翻译规范")
    assert "[[M12]]" in messages[0]["content"]        # 结构规则也带上了
    assert "probe → 探针" in messages[0]["content"]   # 术语在 system 里
    assert "layer → 层" in messages[0]["content"]
    # user 消息必须保持原样：它的模板写死 "Treat next line as plain text input"，
    # 往里塞术语表有被当成待翻译正文的风险
    assert messages[1]["content"].endswith("Hello [[M0]]")
    # 提示词/术语变了必须刷新缓存影响参数，否则会命中旧译文
    assert "prompt" in tr.params and "yiyue_glossary" in tr.params


def test_apply_arxiv_prompts_without_custom_prompt():
    from app.services import babeldoc_runner

    class FakeTranslator:
        def __init__(self):
            self.params: dict = {}

        def prompt(self, text):
            return [{"role": "system", "content": "OLD"},
                    {"role": "user", "content": text}]

        def add_cache_impact_parameters(self, k, v):
            self.params[k] = v

    class P:
        custom_system_prompt = None

    tr = FakeTranslator()
    babeldoc_runner.apply_arxiv_prompts(tr, P(), "zh", [])
    messages = tr.prompt("x")
    assert messages[0]["content"] != "OLD"
    assert "[[M12]]" in messages[0]["content"]   # 没有自定义规范时也要有结构规则
    assert "yiyue_glossary" not in tr.params     # 没有术语就不加缓存参数


# ---------- 并发翻译：顺序、统计合并、取消立刻生效 ----------

def test_map_parallel_preserves_order_and_merges_stats():
    import time as _t

    class Tr:
        def translate(self, text, ignore_cache=False):
            if text.startswith("slow"):
                _t.sleep(0.3)          # 第一块最慢：结果必须仍按输入顺序返回
            return "译 " + text

    chunks = [f"slow {i}" for i in range(1)] + [f"fast {i}" for i in range(5)]
    stats: dict = {}
    out = tex_translate._map_parallel(
        tex_translate._chunk_worker(Tr(), False), chunks, stats, workers=4)
    assert out == ["译 " + c for c in chunks]
    assert stats.get("chunks_untranslated", 0) == 0


def test_map_parallel_merges_failure_stats():
    class Tr:
        def translate(self, text, ignore_cache=False):
            if "BAD" in text:
                raise RuntimeError("boom")
            return "译 " + text

    chunks = ["ok one", "BAD one", "ok two"]
    stats: dict = {}
    out = tex_translate._map_parallel(
        tex_translate._chunk_worker(Tr(), False), chunks, stats, workers=3)
    assert out[0] == "译 ok one"
    assert out[1] is None           # 失败块保留原文
    assert out[2] == "译 ok two"
    assert stats["chunks_untranslated"] == 1


def test_map_parallel_cancel_returns_immediately():
    """取消（progress_cb 抛 TaskCancelled）不能等排队中的块跑完。"""
    import time as _t

    from app.services.orchestrator import TaskCancelled

    class Tr:
        def translate(self, text, ignore_cache=False):
            _t.sleep(0.4)
            return "译 " + text

    chunks = [f"c{i}" for i in range(12)]     # 串行要 ~4.8s，2 并发也要 ~2.4s
    seen = {"n": 0}

    def cb(stage, done, total):
        seen["n"] += 1
        if seen["n"] >= 2:
            raise TaskCancelled()

    start = _t.monotonic()
    with pytest.raises(TaskCancelled):
        tex_translate._map_parallel(
            tex_translate._chunk_worker(Tr(), False), chunks, {},
            workers=2, progress_cb=cb)
    assert _t.monotonic() - start < 2


def test_looks_untranslated_ignores_macro_only_chunks():
    r"""回归：`{HTML}{1B2A32}` 这类纯宏/颜色块没有句子结构，模型原样返回是**正确**
    结果，不该被"等于没翻译"拦下（实测 2607.03728 有 2 块因此白白重试后回退）。"""
    macro_chunk = ("[[M19]] [[M20]] [[M1]]{HTML}{1B2A32} "
                   "[[M2]]{HTML}{0E7C7B} [[M3]]{HTML}{C44E52} [[M4]]{\\textcolor{INERT}}")
    assert not tex_translate._looks_untranslated(macro_chunk, macro_chunk)

    # 成句英文的"原样返回"仍要被抓住
    prose = "The decisive test holds the invariant fixed and changes only the output."
    assert tex_translate._looks_untranslated(prose, prose)
    assert tex_translate._looks_untranslated(
        prose, prose.replace("decisive", "crucial"))


def test_looks_untranslated_ignores_formula_heavy_chunks():
    """回归：占位符（公式/命令）在原文与译文里逐字相同，混进相似度会把
    "公式密集但正文确实翻了"的块算成高相似度，译文被无谓退回英文。"""
    # 一个长块：大量受保护片段 + 少量英文散文，译文照翻了英文
    body = "[[M0]] " * 120 + "We train linear classifiers on the features."
    translated = "[[M0]] " * 120 + "我们在这些特征上训练线性分类器。"
    assert not tex_translate._looks_untranslated(body, translated)

    # 真的"原样返回"仍然要被抓住
    assert tex_translate._looks_untranslated(body, body)
    # 英文单词没减少（只加了个前缀）也算没翻
    assert tex_translate._looks_untranslated(body, "译 " + body)


def test_translate_tex_parallel_covers_all_chunks_and_captions():
    import re as _re

    class ZhTranslator:
        """把英文单词换成中文，模拟真实翻译（保留占位符与命令）。"""

        def translate(self, text, ignore_cache=False):
            return _re.sub(r"[A-Za-z]{2,}", "中", text)

    paras = [(f"Paragraph {i}: " + "some English prose words here. " * 12)
             for i in range(9)]      # 总长 > CHUNK_CHARS，确保切成多块
    tex = ("\\documentclass{article}\n\\title{My Title}\n\\begin{document}\n"
           + "\n\n".join(paras)
           + "\n\n\\begin{figure}\\caption{A caption here}\\end{figure}\n"
           "\\end{document}\n")
    stats: dict = {}
    out = tex_translate.translate_tex(tex, ZhTranslator(), "zh", None, stats)

    assert "[[" not in out                      # 占位符不能漏
    assert "My Title" not in out and "\\title{" in out      # 标题被翻译
    assert "A caption here" not in out                      # 图注被翻译
    assert "prose" not in out                               # 正文被翻译
    assert stats["captions_total"] == 2         # 1 个图注 + 1 个标题
    assert stats["chunks_total"] >= 2           # 正文确实被切成多块
    assert stats.get("chunks_untranslated", 0) == 0
    assert stats.get("captions_untranslated", 0) == 0
    assert (out.count("{"), out.count("}")) == (tex.count("{"), tex.count("}"))


def test_chunk_worker_splits_failed_chunk_and_recovers():
    """整块两次不合格 → 劈成两半各试一次；成功则不再报"未翻译"。"""
    class Picky:
        """整块必失败（模拟占位符被写坏），半块可以成功。"""

        def translate(self, text, ignore_cache=False):
            if len(text) > 600:
                return text + " {broken"        # 结构被破坏
            return "译 " + text

    long_chunk = "English sentence here. " * 40      # > 600 字符
    stats: dict = {}
    out, local = tex_translate._chunk_worker(Picky(), False)(long_chunk)
    assert out is not None                           # 拆分后成功
    assert local.get("chunks_split_recovered") == 1
    assert "chunks_untranslated" not in local
    _ = stats


def test_chunk_worker_keeps_failure_when_split_also_fails():
    class AlwaysBad:
        def translate(self, text, ignore_cache=False):
            return text + " {broken"

    long_chunk = "English sentence here. " * 40
    out, local = tex_translate._chunk_worker(AlwaysBad(), False)(long_chunk)
    assert out is None
    assert local["chunks_untranslated"] >= 1


# ---------- 任务时间：ISO 格式 + 耗时（前端按本地时区显示） ----------

def test_task_json_timestamps_are_iso_with_duration():
    r"""回归：原来 `str(datetime)` 给出 "2026-10-07 14:44:34.507355+00:00"
    （空格分隔 + 6 位微秒），JS 的 new Date() 解析不确定，前端没法稳定显示
    "完成时间"。统一成 ISO-8601 并附上后端算好的耗时。"""
    from datetime import datetime, timedelta, timezone

    from app.api.tasks import _task_json
    from app.models import Task

    created = datetime(2026, 10, 7, 14, 4, 0, tzinfo=timezone.utc)
    finished = created + timedelta(minutes=36, seconds=34)
    task = Task(filename="x.pdf", file_hash="h", size_bytes=1, profile_id=1,
                status="done", created_at=created, finished_at=finished)
    js = _task_json(task)
    assert js["created_at"] == "2026-10-07T14:04:00+00:00"
    assert js["finished_at"] == "2026-10-07T14:40:34+00:00"
    assert js["duration_s"] == 2194.0
    # 能被 JS 标准解析：带 T 分隔符且带时区偏移
    assert "T" in js["finished_at"] and js["finished_at"].endswith("+00:00")

    task.status = "translating"
    task.finished_at = None
    js2 = _task_json(task)
    assert js2["finished_at"] is None and js2["duration_s"] is None


def test_snapshot_carries_engine_and_finish_time():
    """SSE 的 state 事件也要带上引擎与结束时间，否则这些字段只有在整表刷新时才更新。"""
    from datetime import datetime, timezone

    from app.models import Task
    from app.services.orchestrator import TaskManager

    task = Task(id=1, filename="x.pdf", file_hash="h", size_bytes=1, profile_id=1,
                status="done", engine_used="arxiv",
                created_at=datetime(2026, 10, 7, 14, 0, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 10, 7, 14, 10, 0, tzinfo=timezone.utc))
    snap = TaskManager()._snapshot(task)
    assert snap["engine_used"] == "arxiv"
    assert snap["finished_at"] == "2026-10-07T14:10:00+00:00"
    assert snap["duration_s"] == 600.0


def test_timeutil_handles_missing_and_naive_values():
    from app import timeutil

    assert timeutil.iso(None) is None
    assert timeutil.duration_s(None, None) is None
    # 一方带时区一方不带（历史数据）不能抛错
    from datetime import datetime, timezone

    assert timeutil.duration_s(datetime(2026, 10, 7, 14, 0, 0),
                               datetime(2026, 10, 7, 14, 10, 0,
                                        tzinfo=timezone.utc)) is None


# ---------- 验证器：别再误报"整节丢失""漏译" ----------

def test_english_residue_split_classifies_graphics_text():
    """回归：插图里的英文与基线逐字相同，属"保留作者原图"，不是漏译。
    2607.03728 的 fig5 就因此被误报成"成句英文残留"。"""
    graphic_line = "Decodability is uniformly high and does not predict deployment"
    leaked_line = "this whole paragraph was never translated at all here"
    translated = graphic_line + "\n中文正文\n" + leaked_line
    baseline = graphic_line + "\n" + "source text " * 5

    leaked, graphics = verify.english_residue_split(translated, baseline)
    assert graphic_line in graphics
    assert graphic_line not in leaked
    assert leaked_line in leaked


def test_count_section_cmds_ignores_macro_names_and_comments():
    r"""回归：`\sectionname`/`\paragraphname` 这类宏名不能当成章节命令。

    实测它让"源码层章节命令数"平白多出一条（19→20），把一篇结构完好的论文
    判成"标题结构被改动"。
    """
    tex = ("\\providecommand\\sectionname{}\n"
           "\\section{Intro}\n"
           "\\subsection{Sub}\n"
           "\\paragraph{Para}\n"
           "% \\section{commented out}\n"
           "\\section*{Starred}\n"
           "\\sectionname xyz")
    assert tex_translate.count_section_cmds(tex) == 4


def test_section_judgement_prefers_source_counts():
    """有源码层命令数时以它为准：PDF 计数差 4 条也不再报"整节丢失"。"""
    reasons, notes = verify.section_judgement(85, 89, 12, 12)
    assert reasons == []
    assert any("源码层" in n and "无内容丢失" in n for n in notes)

    # 源码层真的少了 → 硬失败
    reasons, _ = verify.section_judgement(85, 89, 12, 11)
    assert reasons and "源码层" in reasons[0]

    # 没有源码计数时退回启发式：差 4 条仍报可疑
    reasons, _ = verify.section_judgement(85, 89, None, None)
    assert reasons and "整节" in reasons[0]

    # 差 1 条只是提示
    reasons, notes = verify.section_judgement(88, 89, None, None)
    assert reasons == [] and notes


def test_chunk_failure_details_are_recorded():
    """失败必须留下可诊断的原因片段（否则报告只会说"有 N 块没翻译"）。"""
    class Tr:
        def translate(self, text, ignore_cache=False):
            raise RuntimeError("endpoint exploded")

    stats: dict = {}
    assert tex_translate._translate_chunk(Tr(), "some english text here", stats) is None
    assert stats["chunks_untranslated"] == 1
    assert "调用失败" in stats["chunks_failed_detail"][0]
    assert "some english text" in stats["chunks_failed_detail"][0]
    # 英文词数让验证器能区分"真丢正文"与"纯结构块被回退"
    assert stats["chunks_failed_english_words"] == 4


def test_verify_distinguishes_structural_revert_from_lost_prose(tmp_path):
    """2 个纯结构块被回退（0 个英文词）→ 提示而不是"未翻译"；真丢正文才报。"""
    import pymupdf

    from app.services import verify as v

    def make(path, text):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), text, fontsize=12)
        doc.save(path)
        doc.close()

    zh, src = tmp_path / "zh.pdf", tmp_path / "src.pdf"
    make(zh, "译文正文示例。" * 40)
    make(src, "Source body text. " * 20)

    structural = v.verify_translation(
        zh, src, tmp_path / "v1",
        issues={"chunks_untranslated": 2, "chunks_failed_english_words": 0,
                "chunks_failed_detail": ["重试后仍不合格（占位符/结构校验）：[[M0]]"]})
    assert not any("未翻译" in r for r in structural["reasons"])
    assert any("纯命令/结构块" in n for n in structural["notes"])

    real_loss = v.verify_translation(
        zh, src, tmp_path / "v2",
        issues={"chunks_untranslated": 1, "chunks_failed_english_words": 250,
                "chunks_failed_detail": ["重试后仍不合格：We train linear probes"]})
    assert any("1 个正文块未翻译" in r for r in real_loss["reasons"])


def test_integrity_ok_catches_dropped_structural_commands():
    r"""回归：`\item`/`\\` 没有花括号，丢一个不会改变 {} 计数，但会真的改变排版
    （两个列表项并成一条、该断的行不断），必须也被拦下。"""
    ok = "\\begin{itemize}\\item one\\item two\\end{itemize}"
    assert tex_translate._integrity_ok(ok, "\\begin{itemize}\\item 一\\item 二\\end{itemize}")
    assert not tex_translate._integrity_ok(ok, "\\begin{itemize}\\item 一\\end{itemize}")

    rows = "a \\\\ b \\\\ c"
    assert tex_translate._integrity_ok(rows, "甲 \\\\ 乙 \\\\ 丙")
    assert not tex_translate._integrity_ok(rows, "甲 \\\\ 乙 丙")


def test_integrity_ok_rejects_placeholder_count_mismatch():
    src = "text [[M0]] more [[M1]]"
    assert tex_translate._integrity_ok(src, "文本 [[M0]] 更多 [[M1]]")
    assert not tex_translate._integrity_ok(src, "文本 [[M0]] 更多")


def test_verify_flags_placeholder_leaks(tmp_path):
    """成品里残留 [[M7]]（PDF 上会印成 [M7]）必须判为需人工复核。"""
    import pymupdf

    from app.services import verify as v

    def make(path, text):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), text, fontsize=12)
        doc.save(path)
        doc.close()

    zh, src = tmp_path / "zh.pdf", tmp_path / "src.pdf"
    make(zh, "正文里漏了占位符 [[M7]] 和 [[C3]]。")
    make(src, "Source body text.")

    report = v.verify_translation(
        zh, src, tmp_path / "verify",
        issues={"placeholder_leaks": ["main.tex:7:[[M7]]", "main.tex:9:[[C3]]"]})
    assert report["verdict"] == "需人工复核"
    assert report["content"]["placeholder_leaks_in_pdf"] == 2
    assert any("残留占位符" in r for r in report["reasons"])
    assert any("占位符字面量" in r for r in report["reasons"])


# ---------- 启动器：只清理我们自己的进程 ----------

def test_free_stale_port_skips_foreign_process(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "launch_mod",
        Path(__file__).resolve().parents[2] / "scripts" / "launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)

    logs: list[str] = []
    killed: list[list[str]] = []
    monkeypatch.setattr(launch, "log", logs.append)
    monkeypatch.setattr(launch, "port_holder_pids", lambda port=launch.PORT: [4242])
    monkeypatch.setattr(launch, "_pid_command_line",
                        lambda pid: '"C:\\other\\app.exe" --serve')
    monkeypatch.setattr(launch.subprocess, "run",
                        lambda *a, **k: killed.append(list(a[0])))
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)

    launch.free_stale_port()
    assert killed == []                       # 不能杀别人的进程
    assert any("不是译阅" in m for m in logs)


def test_free_stale_port_kills_own_server(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "launch_mod2",
        Path(__file__).resolve().parents[2] / "scripts" / "launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)

    monkeypatch.setattr(launch, "log", lambda *_: None)
    monkeypatch.setattr(launch, "port_holder_pids", lambda port=launch.PORT: [777])
    monkeypatch.setattr(
        launch, "_pid_command_line",
        lambda pid: "python.exe -m uvicorn app.main:app --host 127.0.0.1")
    killed: list[list[str]] = []
    monkeypatch.setattr(launch.subprocess, "run",
                        lambda *a, **k: killed.append(list(a[0])))
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)

    launch.free_stale_port()
    assert killed and killed[0][:2] == ["taskkill", "/PID"]


# ---------- BabelDOC 补丁表：幂等标记必须只出现在新串里 ----------

def test_babeldoc_patch_markers_are_new_only():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "abp",
        Path(__file__).resolve().parents[2] / "scripts" / "apply_babeldoc_patches.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # babeldoc 目录是惰性定位的，导入不需要装包

    assert mod.PATCHES
    for rel, replacements in mod.PATCHES:
        for old, new, marker in replacements:
            assert marker in new, f"{rel}: 标记不在新串里，补丁会永远重放"
            assert marker not in old, f"{rel}: 标记已在旧串里，永远判为已应用"
            assert new.count(marker) >= 1


# ---------- 编排器：取消语义 / 提交去重 / SSE 背压 ----------

def _manager():
    from app.services.orchestrator import TaskManager
    return TaskManager()


def test_submit_is_deduplicated():
    m = _manager()
    m.submit(7)
    m.submit(7)
    assert m.queue.qsize() == 1
    assert 7 in m._queued


def test_cancel_does_not_poison_finished_task():
    import asyncio

    m = _manager()
    # 不在队列、也不是当前运行任务 → 不能留下永久取消标记
    assert asyncio.run(m.cancel(3)) is False
    assert 3 not in m._cancel_requested

    m._queued.add(3)
    assert asyncio.run(m.cancel(3)) is True
    assert 3 in m._cancel_requested


def test_worker_loop_maps_taskcancelled_to_cancelled():
    """回归：排队中就取消时 _run_task 直接抛 TaskCancelled，
    以前会被归到 failed 且 error 是空字符串。"""
    import asyncio

    from app.services.orchestrator import TaskCancelled, TaskManager

    async def main():
        m = TaskManager()
        events: list[str] = []

        async def fake_run(task_id: int) -> None:
            raise TaskCancelled()

        m._run_task = fake_run  # 实例属性：不会被绑定 self
        m._set_status = lambda tid, status, **kw: events.append(status)
        m.broadcast = lambda tid, payload: events.append(payload["type"])
        m.submit(11)
        worker = asyncio.create_task(m._worker_loop())
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        return events

    events = asyncio.run(main())
    assert "cancelled" in events
    assert "failed" not in events


def test_broadcast_is_bounded_and_prunes_subscribers():
    from app.services.orchestrator import SUBSCRIBER_QUEUE_SIZE

    m = _manager()
    q = m.subscribe(1)
    for i in range(SUBSCRIBER_QUEUE_SIZE + 20):
        m.broadcast(1, {"type": "progress", "i": i})
    assert q.qsize() <= SUBSCRIBER_QUEUE_SIZE

    m.unsubscribe(1, q)
    assert 1 not in m._subscribers  # 空集合必须删掉，否则只增不减
    # 没有订阅者时广播不应出错
    m.broadcast(1, {"type": "state"})


# ---------- CORS：只放行本机来源 ----------

def test_cors_only_allows_local_origins(client):
    ok = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"

    ok2 = client.get("/api/health", headers={"Origin": "http://127.0.0.1:8000"})
    assert ok2.headers.get("access-control-allow-origin") == "http://127.0.0.1:8000"

    evil = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


# ---------- 配置档探测：不能把密钥发给另一家厂商 ----------

def test_cross_protocol_probe_requires_custom_base_url(monkeypatch):
    from app.api import profiles as profiles_api

    calls: list[str] = []

    def fake_probe(profile, protocol):
        calls.append(protocol)
        return (protocol == "anthropic"), "detail"

    monkeypatch.setattr(profiles_api, "_probe", fake_probe)

    class P:
        protocol = "openai"
        base_url = ""
        api_key = "sk-secret"
        model = "m"

    class FakeSession:
        def get(self, *a):
            return P()

    res = profiles_api.test_profile(999, s=FakeSession())
    assert calls == ["openai"]                 # 不再自动去探 anthropic 官方端点
    assert "suggest_protocol" not in res

    calls.clear()

    class P2(P):
        base_url = "https://gateway.internal/v1"

    class FakeSession2:
        def get(self, *a):
            return P2()

    res2 = profiles_api.test_profile(999, s=FakeSession2())
    assert calls == ["openai", "anthropic"]    # 自定义端点才跨协议试探
    assert res2["suggest_protocol"] == "anthropic"

