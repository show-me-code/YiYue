"""逐段对照阅读 + AI 阅读助手的单元与接口测试。"""

import json
from pathlib import Path

import pymupdf
import pytest
from sqlmodel import Session

from app import config
from app.db import engine
from app.models import Profile, Task
from app.services import chat as chat_service
from app.services import paragraphs


def _make_pdf(path: Path, blocks: list[str]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60
    for text in blocks:
        page.insert_textbox(pymupdf.Rect(72, y, 520, y + 90), text, fontsize=11)
        y += 100
    doc.save(path)
    doc.close()
    return path


EN1 = "The quick brown fox jumps over the lazy dog repeatedly."
EN2 = "We propose a novel method for paragraph level alignment."
ZH1 = "Translated one: quick brown fox jumps over the lazy dog."
ZH2 = "Translated two: a novel method for paragraph alignment."


def _make_long_pdf(path: Path, text: str) -> Path:
    """insert_textbox 写不下就整块丢弃，所以按 1500 字符分页。"""
    doc = pymupdf.open()
    for i in range(0, len(text), 1500):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 760),
                            text[i:i + 1500], fontsize=11)
    doc.save(path)
    doc.close()
    return path


# ---------- 段落抽取与对齐 ----------

def test_extract_filters_short_blocks(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf", [EN1, "Fig. 1", EN2])
    result = paragraphs.extract_paragraphs(pdf)
    assert [r["text"] for r in result] == [EN1, EN2]
    assert all(r["page"] == 1 for r in result)


def test_load_pairs_aligns_and_caches(tmp_path):
    _make_pdf(tmp_path / "original.pdf", [EN1, EN2])
    _make_pdf(tmp_path / "mono.pdf", [ZH1, ZH2])
    data = paragraphs.load_pairs(tmp_path)
    assert data["ready"] is True
    assert data["source_count"] == 2 and data["target_count"] == 2
    assert [(p["source"], p["target"]) for p in data["pairs"]] == [
        (EN1, ZH1), (EN2, ZH2),
    ]
    assert (tmp_path / "paragraphs.json").exists()


def test_load_pairs_not_ready_without_mono(tmp_path):
    _make_pdf(tmp_path / "original.pdf", [EN1])
    data = paragraphs.load_pairs(tmp_path)
    assert data["ready"] is False and data["pairs"] == []


def test_load_pairs_rebuilds_when_mono_changes(tmp_path):
    _make_pdf(tmp_path / "original.pdf", [EN1, EN2])
    _make_pdf(tmp_path / "mono.pdf", [ZH1])
    first = paragraphs.load_pairs(tmp_path)
    assert len(first["pairs"]) == 1
    _make_pdf(tmp_path / "mono.pdf", [ZH1, ZH2])
    second = paragraphs.load_pairs(tmp_path)
    assert len(second["pairs"]) == 2


def test_load_pairs_truncates_to_shorter_side(tmp_path):
    _make_pdf(tmp_path / "original.pdf", [EN1, EN2])
    _make_pdf(tmp_path / "mono.pdf", [ZH1])
    data = paragraphs.load_pairs(tmp_path)
    assert len(data["pairs"]) == 1
    assert data["source_count"] == 2 and data["target_count"] == 1


def test_align_tolerates_head_matter_offset(tmp_path):
    """首页标题块拆分数不同（译文只有译题没有英文题）时，靠锚点+位置对齐不错位。"""
    title_en = "THE OBJECTIVE DECIDES: WHEN A MODEL USES A QUANTITY"
    authors = "Alice Smith & Bob Doe University of Example"
    title_zh = "Translated title: the objective decides everything here."
    abs_zh = "Translated abstract: we show the inference is unsound here."
    # 原文首页 3 块（英文题/作者/正文），译文首页 3 块但位置不同：
    # 作者块逐字相同（作者不翻译），应作为锚点强制配对
    _make_pdf(tmp_path / "original.pdf", [title_en, authors, EN1])
    _make_pdf(tmp_path / "mono.pdf", [title_zh, authors, abs_zh])
    data = paragraphs.load_pairs(tmp_path)
    author_pair = next(p for p in data["pairs"] if p["source"] == authors)
    assert author_pair["target"] == authors
    by_source = {p["source"]: p["target"] for p in data["pairs"]}
    assert by_source[title_en] == title_zh
    assert by_source[EN1] == abs_zh


def test_align_filters_arxiv_stamp(tmp_path):
    _make_pdf(tmp_path / "original.pdf",
              ["arXiv:2607.03728v1 [cs.CE] 4 Jul 2026", EN1])
    _make_pdf(tmp_path / "mono.pdf", [ZH1])
    data = paragraphs.load_pairs(tmp_path)
    assert len(data["pairs"]) == 1
    assert data["pairs"][0]["source"] == EN1


def test_context_text_truncates(tmp_path):
    _make_long_pdf(tmp_path / "original.pdf", "word " * 2000)
    text = paragraphs.context_text(tmp_path, max_chars=500)
    assert len(text) < 600
    assert "已截断" in text


# ---------- arXiv 源码级段落抽取（tex 路径） ----------

_SRC_TEX = r"""\documentclass{article}
\newcommand{\Rsq}{R^2}
\title{The Objective Decides}
\author{Alice \and Bob}
\begin{document}
\maketitle
\lhead{Preprint}

\section{Introduction}

\noindent\textbf{Move 1: presence is not use.} A probe recovers $\phivar$ at
$\Rsq \approx 1$ \citep{alice2020,bob2021}.

\begin{equation}
E = mc^2 \label{eq:e}
\end{equation}

\begin{figure}
\includegraphics[width=\textwidth]{figs/f1}
\caption{A schematic of the method \cite{bob2021}.}
\end{figure}

\begin{algorithm}
\caption{Probe Algorithm}
\begin{algorithmic}[1]
\Require frozen $f_\theta$
\State probe $\phivar$
\end{algorithmic}
\end{algorithm}

As shown in \ref{eq:e} and \cite{alice2020}.
\end{document}
"""

_TGT_TEX = r"""\documentclass{article}
\newcommand{\Rsq}{R^2}
\title{目标决定一切}
\author{Alice \and Bob}
\begin{document}
\maketitle
\lhead{预印本}

\section{引言}

\noindent\textbf{要点 1：存在不等于使用。} 探针在 $\Rsq \approx 1$ 恢复
$\phivar$ \citep{alice2020,bob2021}。

\begin{equation}
E = mc^2 \label{eq:e}
\end{equation}

\begin{figure}
\includegraphics[width=\textwidth]{figs/f1}
\caption{方法示意图 \cite{bob2021}。}
\end{figure}

\begin{algorithm}
\caption{探针算法}
\begin{algorithmic}[1]
\Require 冻结 $f_\theta$
\State 探测 $\phivar$
\end{algorithmic}
\end{algorithm}

如 \ref{eq:e} 与 \cite{alice2020} 所示。
\end{document}
"""

# 1x1 白点 PNG
_PNG_1PX = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _mk_tex_task(tmp_path: Path) -> Path:
    src = tmp_path / "arxiv_src"
    out = tmp_path / "arxiv_out"
    for root in (src, out):
        (root / "figs").mkdir(parents=True)
        (root / "figs" / "f1.png").write_bytes(_PNG_1PX)
    (src / "main.tex").write_text(_SRC_TEX, "utf-8")
    (out / "main.tex").write_text(_TGT_TEX, "utf-8")
    _make_pdf(tmp_path / "original.pdf", [EN1])
    _make_pdf(tmp_path / "mono.pdf", [ZH1])
    return tmp_path


def test_tex_pairs_structure(tmp_path):
    from app.services import tex_paragraphs
    data = tex_paragraphs.tex_pairs(_mk_tex_task(tmp_path))
    assert data is not None and data["mode"] == "tex"
    assert data["macros"].get("\\Rsq") == "R^2"
    pairs = data["pairs"]
    # 标题/作者/章节标题
    assert pairs[0]["kind"] == "heading" and pairs[0]["level"] == 1
    assert pairs[0]["source"] == "The Objective Decides"
    assert pairs[0]["target"] == "目标决定一切"
    assert pairs[1]["source"] == "Alice · Bob"
    headings = [p for p in pairs if p["kind"] == "heading"]
    assert any(h["source"] == "Introduction" and h["target"] == "引言"
               for h in headings)
    # 展示公式成段
    assert any(p["kind"] == "math" and "E = mc^2" in p["source"]
               for p in pairs)
    # 嵌套环境（algorithm 包 algorithmic）不重复抽取
    codes = [p for p in pairs if p["kind"] == "code"]
    assert len(codes) == 1
    assert "probe $\\phivar$" in codes[0]["source"]
    assert any(p["kind"] == "caption" and p["source"] == "Probe Algorithm"
               for p in pairs)


def test_tex_pairs_math_and_cites(tmp_path):
    from app.services import tex_paragraphs
    data = tex_paragraphs.tex_pairs(_mk_tex_task(tmp_path))
    move = next(p for p in data["pairs"] if "Move 1" in p["source"])
    # 数学段完整保留（不被清理动作破坏）
    assert "$\\Rsq \\approx 1$" in move["source"]
    assert "$\\phivar$" in move["source"]
    # 引用按首次出现顺序编号
    assert "⟦1,2⟧" in move["source"]
    # \noindent\textbf 不能吞字
    assert move["source"].startswith("Move 1: presence is not use.")
    # \lhead{Preprint} 整体丢弃，不留 {Preprint} 残壳
    assert all("Preprint" not in p["source"] for p in data["pairs"])
    # \ref 渲染为交叉引用标记
    tail = data["pairs"][-1]
    assert "⟦§:eq:e⟧" in tail["source"]
    assert "⟦1⟧" in tail["source"]  # alice2020 已编号为 1


def test_load_pairs_prefers_tex(tmp_path):
    task_dir = _mk_tex_task(tmp_path)
    data = paragraphs.load_pairs(task_dir)
    assert data["mode"] == "tex"
    # 缓存命中同样返回 tex 结果
    again = paragraphs.load_pairs(task_dir)
    assert again["mode"] == "tex"


def test_tex_pairs_figure_images(tmp_path):
    from app.services import tex_paragraphs
    data = tex_paragraphs.tex_pairs(_mk_tex_task(tmp_path))
    fig = next(p for p in data["pairs"] if p["kind"] == "figure")
    # 不带扩展名的 \includegraphics{figs/f1} 解析到实际文件
    assert fig["images"] == ["figs/f1.png"]
    # 图注：先英文后中文，引用编号保留
    assert fig["source"].startswith("A schematic of the method")
    assert "⟦2⟧" in fig["source"]  # bob2021 是全文第 2 个被引文献
    assert fig["target"].startswith("方法示意图")


def test_figimage_api(client, profile, tmp_path):
    task = _mk_task(profile["id"])
    task_dir = config.TASKS_DIR / str(task.id)
    figs = task_dir / "arxiv_out" / "figs"
    figs.mkdir(parents=True)
    (figs / "f1.png").write_bytes(_PNG_1PX)
    ok = client.get(f"/api/tasks/{task.id}/figimage?path=figs/f1.png")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"
    assert client.get(
        f"/api/tasks/{task.id}/figimage?path=../app.db").status_code == 400
    assert client.get(
        f"/api/tasks/{task.id}/figimage?path=figs/none.png").status_code == 404


def test_chat_history_roundtrip(client, profile):
    task = _mk_task(profile["id"])
    assert client.get(
        f"/api/tasks/{task.id}/chat/history").json()["messages"] == []
    msgs = [{"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你？"}]
    r = client.put(f"/api/tasks/{task.id}/chat/history",
                   json={"messages": msgs})
    assert r.status_code == 200
    assert client.get(
        f"/api/tasks/{task.id}/chat/history").json()["messages"] == msgs
    # 长回答（超 8000 字符的总结类内容）也必须能存——之前沿用问答校验会 400 丢失
    long_msgs = [{"role": "assistant", "content": "长" * 20000}]
    r = client.put(f"/api/tasks/{task.id}/chat/history",
                   json={"messages": long_msgs})
    assert r.status_code == 200
    assert len(client.get(f"/api/tasks/{task.id}/chat/history")
              .json()["messages"][0]["content"]) == 20000
    # 清空会话 = 保存空列表（允许空）
    client.put(f"/api/tasks/{task.id}/chat/history", json={"messages": []})
    assert client.get(
        f"/api/tasks/{task.id}/chat/history").json()["messages"] == []
    assert client.get("/api/tasks/9999/chat/history").status_code == 404
    assert client.put("/api/tasks/9999/chat/history",
                      json={"messages": []}).status_code == 404


# ---------- 翻译风格预设 ----------

def test_polish_presets():
    from app.services import prompt_presets as pp

    # 默认（学术规范）也带风格指令：长句拆分、避免欧式中文
    default_eff = pp.effective_custom_prompt(None, "default")
    assert default_eff and "学术" in default_eff
    eff_custom = pp.effective_custom_prompt("自定义规范", "default")
    assert eff_custom.endswith("自定义规范") and "学术" in eff_custom
    assert "润色" in pp.effective_custom_prompt(None, "polished")
    eff = pp.effective_custom_prompt("用户规范", "faithful")
    assert "忠实直译" in eff and eff.endswith("用户规范")  # 用户规范在后
    assert pp.effective_custom_prompt(None, "不存在的key") == \
        pp.effective_custom_prompt(None, "default")


def test_create_task_polish(client, profile, tmp_path):
    pdf = _make_pdf(tmp_path / "p.pdf", [EN1])
    bad = client.post("/api/tasks",
                      files={"file": ("p.pdf", pdf.read_bytes(),
                                      "application/pdf")},
                      data={"profile_id": profile["id"], "polish": "bad"})
    assert bad.status_code == 400
    ok = client.post("/api/tasks",
                     files={"file": ("p.pdf", pdf.read_bytes(),
                                     "application/pdf")},
                     data={"profile_id": profile["id"], "polish": "polished"})
    assert ok.status_code == 200
    assert ok.json()["polish"] == "polished"
    # 任务跑完或失败后清理，不影响其他用例
    client.post(f"/api/tasks/{ok.json()['id']}/cancel")


# ---------- 归档与元数据增强 ----------

def test_archive_toggle(client, profile):
    task = _mk_task(profile["id"])
    r = client.post(f"/api/tasks/{task.id}/archive", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True
    detail = client.get(f"/api/tasks/{task.id}").json()
    assert detail["archived"] is True
    assert "title_en" in detail and "topics" in detail
    r = client.post(f"/api/tasks/{task.id}/archive", json={"archived": False})
    assert r.json()["archived"] is False
    assert client.post("/api/tasks/9999/archive",
                       json={"archived": True}).status_code == 404


def test_parse_enrich_reply():
    from app.services import enrich

    title, topics = enrich.parse_enrich_reply(
        "中文标题：目标决定一切：模型何时使用守恒量\n主题：可解释性、线性探针、动力学模型")
    assert title == "目标决定一切：模型何时使用守恒量"
    assert topics == ["可解释性", "线性探针", "动力学模型"]
    assert enrich.parse_enrich_reply("无法解析的回复") == (None, [])


def test_enrich_offline_titles(client, profile):
    """有 tex 源码的任务：标题离线提取；mock LLM 只回声，主题为空但不崩。"""
    from app.services import enrich

    task = _mk_task(profile["id"])
    task_dir = config.TASKS_DIR / str(task.id)
    (task_dir / "arxiv_src").mkdir(exist_ok=True)
    (task_dir / "arxiv_out").mkdir(exist_ok=True)
    (task_dir / "arxiv_src" / "main.tex").write_text(_SRC_TEX, "utf-8")
    (task_dir / "arxiv_out" / "main.tex").write_text(_TGT_TEX, "utf-8")
    enrich.enrich_task(task.id)
    detail = client.get(f"/api/tasks/{task.id}").json()
    assert detail["title_en"] == "The Objective Decides"
    assert detail["title_zh"] == "目标决定一切"
    assert detail["topics"] == []  # 回声 mock 不产生主题词，但不报错


# ---------- AI 问答服务 ----------

def _profile(protocol: str = "openai") -> Profile:
    # Anthropic 协议的 base_url 约定不含 /v1（与 anthropic_translator 一致）
    base = "http://test" if protocol == "anthropic" else "http://test/v1"
    return Profile(name="t", base_url=base, api_key="k",
                   model="m", protocol=protocol)


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_chat_openai(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):  # noqa: A002
        captured.update(url=url, headers=headers, body=json)
        return _FakeResp({"choices": [{"message": {"content": "回答"}}]})

    monkeypatch.setattr(chat_service.httpx, "post", fake_post)
    reply = chat_service.chat_reply(
        _profile(), "paper text", [{"role": "user", "content": "你好"}])
    assert reply == "回答"
    assert captured["url"] == "http://test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    msgs = captured["body"]["messages"]
    assert msgs[0]["role"] == "system" and "paper text" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "你好"}


def test_chat_anthropic(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):  # noqa: A002
        captured.update(url=url, headers=headers, body=json)
        return _FakeResp({"content": [{"type": "text", "text": "回答"}]})

    monkeypatch.setattr(chat_service.httpx, "post", fake_post)
    reply = chat_service.chat_reply(
        _profile("anthropic"), "paper text",
        [{"role": "user", "content": "你好"}])
    assert reply == "回答"
    assert captured["url"] == "http://test/v1/messages"
    assert captured["headers"]["x-api-key"] == "k"
    assert "paper text" in captured["body"]["system"]
    assert all(m["role"] in ("user", "assistant")
               for m in captured["body"]["messages"])


def test_chat_http_error(monkeypatch):
    def fake_post(url, headers, json, timeout):  # noqa: A002
        resp = _FakeResp({"error": {"message": "bad key"}})
        resp.status_code = 401
        return resp

    monkeypatch.setattr(chat_service.httpx, "post", fake_post)
    with pytest.raises(chat_service.ChatError, match="401"):
        chat_service.chat_reply(_profile(), "", [{"role": "user", "content": "hi"}])


# ---------- API 接口 ----------

def _mk_task(profile_id: int) -> Task:
    with Session(engine) as s:
        task = Task(filename="paper.pdf", file_hash="h", size_bytes=1,
                    profile_id=profile_id, status="done")
        s.add(task)
        s.commit()
        s.refresh(task)
        task_dir = config.TASKS_DIR / str(task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        _make_pdf(task_dir / "original.pdf", [EN1, EN2])
        _make_pdf(task_dir / "mono.pdf", [ZH1, ZH2])
        return task


def test_paragraphs_api(client, profile):
    task = _mk_task(profile["id"])
    resp = client.get(f"/api/tasks/{task.id}/paragraphs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["pairs"][0]["source"] == EN1
    assert data["pairs"][0]["target"] == ZH1


def test_paragraphs_api_404(client):
    assert client.get("/api/tasks/9999/paragraphs").status_code == 404


def test_chat_api(client, profile):
    # mock_llm 是回声服务：回复 = 最后一条 user 消息内容（去掉系统前缀后的原文）
    task = _mk_task(profile["id"])
    resp = client.post(f"/api/tasks/{task.id}/chat", json={
        "messages": [{"role": "user", "content": "总结这篇论文"}],
    })
    assert resp.status_code == 200
    assert resp.json()["reply"] == "总结这篇论文"


def test_chat_api_rejects_bad_messages(client, profile):
    task = _mk_task(profile["id"])
    assert client.post(f"/api/tasks/{task.id}/chat",
                       json={"messages": []}).status_code == 400
    assert client.post(f"/api/tasks/{task.id}/chat", json={
        "messages": [{"role": "assistant", "content": "x"}],
    }).status_code == 400
    assert client.post(f"/api/tasks/{task.id}/chat", json={
        "messages": [{"role": "hacker", "content": "x"}],
    }).status_code == 400
