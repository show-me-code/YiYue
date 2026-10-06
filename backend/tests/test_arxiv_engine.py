"""arXiv 引擎单元测试：ID 检测 / 保护恢复 / 字体注入 / 主文件定位。"""

import textwrap

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
    assert tex_translate._restore("{[[M0]]}", prot) == "$x^2$"
    # 越界占位符原样保留
    assert tex_translate._restore("[[M99]]", prot) == "[[M99]]"


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
