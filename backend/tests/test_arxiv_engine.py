"""arXiv 引擎单元测试：ID 检测 / 保护恢复 / 字体注入 / 主文件定位 / 闭环验证。"""

import textwrap

import pymupdf

from app.services import arxiv_engine, tex_translate


def test_detect_arxiv_id_variants():
    assert arxiv_engine.detect_arxiv_id(user_input="1610.01644") == "1610.01644"
    assert arxiv_engine.detect_arxiv_id(
        user_input="https://arxiv.org/abs/2401.12345v2") == "2401.12345v2"
    assert arxiv_engine.detect_arxiv_id(
        filename="Alain 2018 1610.01644v4.pdf") == "1610.01644v4"
    assert arxiv_engine.detect_arxiv_id(user_input="cs/0112017") == "cs/0112017"
    # 普通文本不应误报
    assert arxiv_engine.detect_arxiv_id(user_input="hello world 2024") is None


def test_protect_and_restore_roundtrip():
    tex = textwrap.dedent("""\\
        \\section{Introduction}
        The loss $L_k$ is defined in Equation~\\eqref{eq:1}:
        \\begin{equation}\\label{eq:1}
          L = \\sum_i x_i^2 % main term
        \\end{equation}
        As shown in Figure~\\ref{fig:a} and \\cite{Alain2018}, deep \\emph{probes}
        are useful. See \\url{https://example.com}.
    """)
    protected, prot = tex_translate._build_protection(tex)
    # 占位符出现
    assert "[[M0]]" in protected
    # 公式、引用命令被完整保护
    assert any("\\begin{equation}" in p for p in prot)
    assert any("\\cite{Alain2018}" in p for p in prot)
    assert any("\\url{https://example.com}" in p for p in prot)
    # 恢复后与原文一致
    assert tex_translate._restore(protected, prot) == tex


def test_restore_tolerates_mangled_placeholders():
    prot = ["$x^2$"]
    assert tex_translate._restore("[[ M0 ]]", prot) == "$x^2$"
    # 源码里包裹占位符的花括号是真实 LaTeX 语法，必须原样保留：
    # 曾因为把 {[[M0]]} 整体替换，产出 \hbox$x$ 这种坏文件（编译 Missing {）
    assert tex_translate._restore("{[[M0]]}", prot) == "{$x^2$}"
    assert tex_translate._restore("\\hbox{[[M0]]}.", prot) == "\\hbox{$x^2$}."
    # 越界占位符原样保留
    assert tex_translate._restore("[[M99]]", prot) == "[[M99]]"


def test_translate_tex_keeps_braces_around_math():
    r"""回归：\hbox{$...$} 这类"花括号紧贴受保护片段"的写法不能被恢复逻辑吃掉。"""

    class PassThroughTranslator:
        def translate(self, text, ignore_cache=False):
            return "中文" + text

    tex = ("\\begin{document}\nResNet models map essentially "
           "\\hbox{$x \\mapsto x + r(x)$}.\n\\end{document}\n")
    out = tex_translate.translate_tex(tex, PassThroughTranslator(), "zh")
    assert "\\hbox{$x \\mapsto x + r(x)$}" in out
    assert out.count("{") == out.count("}") == tex.count("{")


def test_translate_tex_keeps_structure(monkeypatch):
    class FakeTranslator:
        def translate(self, text):
            # 模拟 LLM：把英文单词替换为中文标记，保留其它行
            out_lines = []
            for line in text.splitlines():
                if line.strip() and not line.lstrip().startswith(("\\", "[[", "%")):
                    out_lines.append("译|" + line)
                else:
                    out_lines.append(line)
            return "\n".join(out_lines)

    tex = textwrap.dedent("""\\
        \\documentclass{article}
        \\title{My Paper}
        \\begin{document}
        \\section{Intro}

        Hello world, this is a test paragraph about $x^2$ and \\cite{a}.

        Second paragraph.
        \\end{document}
    """)
    out = tex_translate.translate_tex(tex, FakeTranslator(), "zh")
    assert "\\documentclass{article}" in out
    assert "\\begin{document}" in out and "\\end{document}" in out
    assert "译|Hello world" in out or "译| Second paragraph" in out or "译|" in out
    assert "$x^2$" in out and "\\cite{a}" in out
    # title 被翻译
    assert "译|My Paper" in out


def test_prepare_for_xetex():
    tex = "\\documentclass{article}\n\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n\\begin{document}x\\end{document}\n"
    out = tex_translate.prepare_for_xetex(tex, "zh")
    assert "inputenc" not in out and "fontenc" not in out
    assert "ctex" in out and "fandol" in out
    assert "\\renewcommand\\figurename{图}" in out
    # 非 zh 不注入
    out2 = tex_translate.prepare_for_xetex(tex, "en")
    assert "ctex" not in out2


def test_skeleton_integrity():
    from app.services.tex_translate import _integrity_ok
    src = "text \\hbox{[[M1]]} more [[M2]] $y$"
    ok = "中文 \\hbox{[[M1]]} 更多 [[M2]] $y$"
    # LLM 吞掉花括号（即使在别处补偿使总数不变）→ 必须检出
    bad = "中文 \\hbox[[M1]] 的 {额外} [[M2]] $y$"
    assert _integrity_ok(src, ok)
    assert not _integrity_ok(src, bad)


def test_find_main_tex(tmp_path):
    main = tmp_path / "main.tex"
    body = tmp_path / "body.tex"
    body.write_text("some text", "utf-8")
    main.write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{body}\n\\end{document}\n",
        "utf-8")
    helper = tmp_path / "helper.tex"
    helper.write_text("\\documentclass{article} standalone", "utf-8")
    assert arxiv_engine.find_main_tex([body, helper, main]) == main
    files = arxiv_engine.collect_doc_files(main, [body, helper, main])
    assert main in files and body in files and helper not in files


# ---- 第 3 步：中文重编译与自动标签汉化 ----

def test_patch_style_files_localizes_nips_abstract(tmp_path):
    """nips 样式写的是 {\\large\\bf Abstract}，旧正则 \\{Abstract\\} 匹配不到。"""
    sty = tmp_path / "nips_2016_nomarkings.sty"
    sty.write_text(
        "\\renewenvironment{abstract}%\n{"
        "\\vskip 0.075in%\n\\centerline%\n{\\large\\bf Abstract}%\n"
        "\\vspace{0.5ex}%\n\\begin{quote}%\n}\n{\\par\\end{quote}}\n",
        "utf-8")
    assert tex_translate.patch_style_files(tmp_path) == 1
    out = sty.read_text("utf-8")
    assert "摘要" in out and "Abstract" not in out


def test_patch_style_files_handles_textbf_and_braces(tmp_path):
    cls = tmp_path / "x.cls"
    cls.write_text("\\def\\abstractname{Abstract}\n\\textbf{Abstract}\n", "utf-8")
    assert tex_translate.patch_style_files(tmp_path) == 1
    out = cls.read_text("utf-8")
    assert out.count("摘要") == 2 and "Abstract" not in out


def test_prepare_for_xetex_restores_linespread_and_localizes_last():
    tex = ("\\documentclass{article}\n"
           "\\renewcommand{\\figurename}[0]{Figure~}\n"
           "\\begin{document}\nbody\n\\end{document}\n")
    out = tex_translate.prepare_for_xetex(tex, "zh")
    # ctex 用 Fandol 字体集
    assert "\\usepackage[fontset=fandol]{ctex}" in out
    # ctex 会放大行距，必须记录并还原（否则页数会变）
    assert "\\edef\\yiyuekeptstretch{\\baselinestretch}" in out
    assert "\\linespread{\\yiyuekeptstretch}\\selectfont" in out
    # 汉化块必须晚于作者自己的 \renewcommand{\figurename}，否则汉化被覆盖回英文
    assert out.index("\\renewcommand\\figurename{图}") > out.index("\\renewcommand{\\figurename}[0]{Figure~}")
    assert out.index("\\renewcommand\\figurename{图}") < out.index("\\begin{document}")
    assert "\\renewcommand\\abstractname{摘要}" in out
    assert "\\renewcommand\\refname{参考文献}" in out


# ---- 第 1 步：用作者自带的 .bbl ----

def test_use_shipped_bbl_rewrites_bibliography(tmp_path):
    (tmp_path / "main.bbl").write_text("\\begin{thebibliography}{1}\n\\end{thebibliography}\n", "utf-8")
    main = tmp_path / "main.tex"
    main.write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\bibliography{alain17a}\n\\bibliographystyle{natbib}\n\\end{document}\n",
        "utf-8")
    info = arxiv_engine.use_shipped_bbl(tmp_path, main)
    assert info["applied"] and info["mapping"] == [["alain17a", "main.bbl"]]
    # 名字对齐 + 改写为 \input，避免 Tectonic 跑 BibTeX 覆盖 .bbl
    assert (tmp_path / "alain17a.bbl").exists()
    out = main.read_text("utf-8")
    assert "\\input{alain17a.bbl}" in out and "\\bibliography{alain17a}" not in out


def test_use_shipped_bbl_without_bbl(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text("\\bibliography{x}\n", "utf-8")
    info = arxiv_engine.use_shipped_bbl(tmp_path, main)
    assert not info["applied"] and "没有 .bbl" in info["reason"]
    assert "\\bibliography{x}" in main.read_text("utf-8")


# ---- 图注抽取：captionof / subcaption / 嵌套花括号 / 子图注补译 ----

def test_extract_captionof_and_nested_braces():
    tex = ("\\captionof{figure}{A \\textbf{bold \\emph{nested}} caption}\n"
           "\\caption[short]{Plain caption}\n"
           "%\\caption{注释掉的标题不要动}\n")
    out, caps = tex_translate._extract_translatable_blocks(
        tex, "C", ("caption", "captionof"))
    assert caps == ["A \\textbf{bold \\emph{nested}} caption", "Plain caption"]
    assert "[[C0]]" in out and "[[C1]]" in out
    assert "\\captionof{figure}{[[C0]]}" in out
    assert "%\\caption{注释掉的标题不要动}" in out  # 注释原样保留


def test_extract_subcaption_uses_caption_channel():
    r"""\subcaption{...} 与 \caption 同走占位符通道（花括号安全）。"""
    tex = ("\\subcaption{Validation errors for probes.}\n"
           "\\captionof{figure}{Outer caption}\n"
           "\\subfloat[Pre-trained ResNet-50 on ImageNet]{\\includegraphics{a.png}}\n")
    out, caps = tex_translate._extract_translatable_blocks(
        tex, "C", ("caption", "captionof", "subcaption"))
    assert caps == ["Validation errors for probes.", "Outer caption"]
    assert "\\subcaption{[[C0]]}" in out
    # subfloat 的可选参数不抽取（方括号里放 [[C1]] 会让 LaTeX 提前结束可选参数）
    assert "\\subfloat[Pre-trained ResNet-50 on ImageNet]" in out


def test_translate_tex_translates_captionof():
    class FakeTranslator:
        def translate(self, text, ignore_cache=False):
            return "译" + text

    tex = "\\begin{document}\n\\captionof{table}{Results are good.}\n\\end{document}\n"
    out = tex_translate.translate_tex(tex, FakeTranslator(), "zh")
    assert "\\captionof{table}{译Results are good.}" in out


# ---- 章节标题 / 子图注兜底补译 ----

def test_translate_english_titles_only_touches_english():
    """锚点补译：只补纯英文标题；已是中文的标题不再发送给 LLM。"""

    class T:
        def __init__(self):
            self.sent = []

        def translate(self, text, ignore_cache=False):
            self.sent.append(text.strip())
            return {"Introduction": "引言", "ResNet-50": "ResNet-50"}.get(
                text.strip(), "其他")

    tr = T()
    tex = "\\section{Introduction}\n\\section{ResNet-50}\n\\section{相关工作}\n"
    stats: dict = {}
    out = tex_translate._translate_english_titles(tex, tr, stats)
    assert "\\section{引言}" in out
    assert "\\section{相关工作}" in out and "相关工作" not in tr.sent
    assert tr.sent == ["Introduction", "ResNet-50"]
    assert stats["labels_retranslated"] == 1  # ResNet-50 返回相同，不计入


def test_translate_english_labels_covers_subfloat():
    """子图注在 figure 环境里受保护，只能靠末尾补译：验证它能认出来并替换。"""

    class T:
        def translate(self, text, ignore_cache=False):
            return {"Pre-trained ResNet-50 on ImageNet dataset.": "在 ImageNet 数据集上预训练的 ResNet-50。",
                    "Introduction": "引言"}.get(text.strip(), text)

    tex = ("\\section{Introduction}\n"
           "\\subfloat[Pre-trained ResNet-50 on ImageNet dataset.]{\\includegraphics{a}}\n")
    stats: dict = {}
    out = tex_translate._translate_english_titles(tex, T(), stats)
    assert "\\subfloat[在 ImageNet 数据集上预训练的 ResNet-50。]" in out
    assert "\\section{引言}" in out
    assert stats["labels_retranslated"] == 2


def test_translate_tex_retranslates_leaked_english_title():
    """LLM 在正文块里漏译短标题时的兜底（实测 \\section{Introduction} 会被原样留下）。"""

    class LeakyTitleTranslator:
        def translate(self, text, ignore_cache=False):
            if text.strip() == "Introduction":
                return "引言"
            out = "中文正文段落。"
            if "\\section{Introduction}" in text:
                out += "\\section{Introduction}"
            return out

    tex = "\\begin{document}\n\\section{Introduction}\n\nSome body text here.\n\\end{document}\n"
    stats: dict = {}
    out = tex_translate.translate_tex(
        tex, LeakyTitleTranslator(), "zh", None, stats)
    assert "\\section{引言}" in out
    assert stats["labels_retranslated"] == 1


# ---- 未翻译返回（历史坏缓存）必须被发现并绕过缓存重试 ----

def test_identity_translation_retries_bypassing_cache():
    class StaleCacheTranslator:
        """第一次原样返回（模拟坏缓存），第二次绕过缓存后真正翻译。"""

        def __init__(self):
            self.calls = []

        def translate(self, text, ignore_cache=False):
            self.calls.append(ignore_cache)
            return text if not ignore_cache else "这段落含有若干英文单词，必须翻译成中文。"

    tr = StaleCacheTranslator()
    chunk = "This paragraph has several English words that must be translated."
    stats: dict = {}
    got = tex_translate._translate_chunk(tr, chunk, stats)
    assert got == "这段落含有若干英文单词，必须翻译成中文。"
    assert stats["identity_retries"] == 1
    assert tr.calls == [False, True]


def test_untranslated_chunk_is_counted():
    class BrokenTranslator:
        def translate(self, text, ignore_cache=False):
            return text  # 永远原样返回

    stats: dict = {}
    chunk = "Another paragraph with enough English words to be suspicious."
    assert tex_translate._translate_chunk(BrokenTranslator(), chunk, stats) is None
    assert stats["chunks_untranslated"] == 1


# ---- 第 5 步：闭环验证 ----

def _make_pdf(path, items):
    """items: [(page_no, x, y, text), ...]，用于构造可控墨迹位置的 PDF。

    文本只用 ASCII：pymupdf 默认字体画不出中文，写进去也提取不出来。
    """
    doc = pymupdf.open()
    pages: dict[int, list] = {}
    for item in items:
        pages.setdefault(item[0], []).append(item[1:])
    for page_no in sorted(pages):
        page = doc.new_page()
        for x, y, text in pages[page_no]:
            page.insert_text((x, y), text)
    doc.save(path)
    doc.close()


def test_verify_passes_and_notes_layout_shift(tmp_path):
    """版心由基线估计：内容位移只作为"需目检"的提示，并生成放大对照图。"""
    from app.services import verify

    base = tmp_path / "baseline.pdf"
    moved = tmp_path / "translated.pdf"
    _make_pdf(base, [(1, 72, 100, "Body text here"), (1, 72, 700, "References"),
                     (2, 72, 100, "Second page text"), (2, 72, 700, "References")])
    _make_pdf(moved, [(1, 72, 160, "Translated body"), (1, 72, 700, "References"),
                      (2, 72, 100, "Second page zh"), (2, 72, 700, "References")])
    report = verify.verify_translation(moved, base, tmp_path / "verify")
    assert report["verdict"] == "通过", report["reasons"]
    assert report["overflow"]["new_pages"] == []
    assert report["page_detail"][0]["delta_vs_baseline_pt"] > 25
    assert any("放大对照图" in n for n in report["notes"])
    assert (tmp_path / "verify" / "render" / "p01.png").exists()
    assert (tmp_path / "verify" / "zoom" / "p01_zh.png").exists()


def test_verify_flags_new_overflow_and_missing_captions(tmp_path):
    """超出基线版心 + 图表标题数变少 = 真问题，必须判为需人工复核。"""
    from app.services import verify

    base = tmp_path / "baseline.pdf"
    bad = tmp_path / "translated.pdf"
    _make_pdf(base, [(1, 72, 100, "Figure 1: a caption"), (1, 72, 700, "References")])
    _make_pdf(bad, [(1, 10, 120, "text outside the left margin"), (1, 72, 700, "References")])
    report = verify.verify_translation(
        bad, base, tmp_path / "verify", issues={"chunks_untranslated": 2})
    assert report["verdict"] == "需人工复核"
    assert report["overflow"]["new_pages"] == [1]
    reasons = " ".join(report["reasons"])
    assert "超出基线版心" in reasons
    assert "图表标题数少于基线" in reasons
    assert "未翻译" in reasons


def test_verify_preserves_author_overflow_as_note(tmp_path):
    """基线自己就溢出的页：属于作者原版瑕疵，只提示、不判失败。"""
    from app.services import verify

    base = tmp_path / "baseline.pdf"
    zh = tmp_path / "translated.pdf"
    # 三页基线：两页正常版心，一页图表超宽 —— 版心取中位数，超宽页才显得出来
    for path, wide in ((base, "Figure 1: wide figure"), (zh, "Tu 1: wide figure")):
        _make_pdf(path, [(1, 72, 100, "Normal page text"), (1, 72, 700, "References"),
                         (2, 20, 100, wide), (2, 72, 700, "References"),
                         (3, 72, 100, "Another normal page"), (3, 72, 700, "References")])
    report = verify.verify_translation(zh, base, tmp_path / "verify")
    assert report["overflow"]["baseline_pages"] == [2]
    assert report["overflow"]["translated_pages"] == [2]
    assert report["overflow"]["new_pages"] == []
    assert any("原版瑕疵" in n for n in report["notes"])


def test_verify_detects_untranslated_english_section(tmp_path):
    """整节退回英文（编译降级）这种失败，条数比对看不出来，必须靠语言检测抓住。"""
    from app.services import verify

    base = tmp_path / "baseline.pdf"
    zh = tmp_path / "translated.pdf"
    _make_pdf(base, [(1, 72, 100, "Figure 1: caption"), (1, 72, 700, "References")])
    _make_pdf(zh, [(1, 72, 100, "Figure 1: caption"),
                   (1, 72, 116, "The family of ResNet models are characterized by their large"),
                   (1, 72, 132, "quantities of residual layers mapping essentially"),
                   (1, 72, 148, "The family of ResNet models are characterized by their large"),
                   (1, 72, 164, "quantities of residual layers mapping essentially"),
                   (1, 72, 180, "The family of ResNet models are characterized by their large"),
                   (1, 72, 700, "References")])
    report = verify.verify_translation(
        zh, base, tmp_path / "verify", issues={"files_reverted": ["experiments.tex"]})
    assert report["verdict"] == "需人工复核"
    assert report["content"]["english_lines"] >= 3
    reasons = " ".join(report["reasons"])
    assert "成句英文" in reasons and "退回英文原文" in reasons


def test_verify_flags_english_caption_in_rendered_pdf(tmp_path):
    """渲染后仍是英文的图表标题要判失败；正文里的行内引用不算。"""
    from app.services import verify

    base = tmp_path / "b.pdf"
    zh = tmp_path / "z.pdf"
    _make_pdf(base, [(1, 72, 100, "Figure 1: A long English caption here"),
                     (1, 72, 700, "References")])
    _make_pdf(zh, [(1, 72, 100, "tu 1: A long English caption here"),
                   (1, 72, 116, "Figure 6 of Raghu et al. (2017b) shows this"),
                   (1, 72, 700, "References")])
    report = verify.verify_translation(zh, base, tmp_path / "verify")
    assert report["content"]["english_captions"] == 1
    assert any("图表标题仍是英文" in r for r in report["reasons"])


def test_cache_key_changes_with_pipeline_version():
    from app.services import cache

    base = {"lang_out": "zh", "engine": "arxiv",
            "pipeline_version": cache.PIPELINE_VERSION}
    old = dict(base, pipeline_version="0")
    assert (cache.compute_cache_key("abc", base)
            != cache.compute_cache_key("abc", old))


def test_section_count_tolerates_wrapped_headings():
    """中文断行会把"3.1 相关工作"拆成两行，计数不能因此少算。"""
    from app.services.verify import section_count

    joined = "1 Introduction\n2 相关工作\n3.1 探针\n"
    wrapped = "1\nIntroduction\n2\n相关工作\n3.1\n探针\n"
    assert section_count(joined) == 3
    assert section_count(wrapped) == 3
    # 坐标轴刻度之类的纯数字行不算章节
    assert section_count("0.00\n0.02\n0.04\n") == 0
