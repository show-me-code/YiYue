"""任务元数据增强：论文标题（中/英）与主题词。

标题离线可得：arXiv 任务从原文/译文的 .tex 导言区 \\title 提取；其余用文件名。
主题词需要 LLM：用任务所属配置档对正文节选做一次问答，输出"中文标题 + 主题词"。
全程 best-effort：LLM 失败时保留离线标题、主题为空，绝不影响任务本身。

调用时机：任务完成时（两条引擎路径 + 缓存命中），以及应用启动时对
历史已完成且缺标题的任务回填（后台线程逐个来，不阻塞启动）。
"""

import json
import logging
import re
import threading
from pathlib import Path

from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import Profile, Task

logger = logging.getLogger(__name__)


def _tex_title(root: Path) -> str | None:
    """从 tex 源码导言区提取标题（抽取器会清理 LaTeX 命令）。"""
    if not root.is_dir():
        return None
    from app.services import tex_paragraphs, textio
    from app.services.arxiv_engine import find_main_tex

    texs = sorted(root.rglob("*.tex"))
    if not texs:
        return None
    try:
        main = find_main_tex(texs)
        text = textio.load_text(main)
    except Exception:  # noqa: BLE001
        return None
    fm = tex_paragraphs._front_matter(text, tex_paragraphs._CiteIndex())
    for p in fm:
        if p["kind"] == "heading" and p["level"] == 1 and p["text"]:
            return p["text"]
    return None


def parse_enrich_reply(reply: str) -> tuple[str | None, list[str]]:
    """解析 LLM 输出的两行格式：中文标题：…／主题：…。容错：找不到就留空。"""
    title_zh = None
    m = re.search(r"中文标题[：:]\s*(.+)", reply)
    if m:
        cand = m.group(1).strip().strip("《》\"'")[:80]
        if cand and "<" not in cand:  # 回声/复读提示词模板时形如 <论文标题…>，丢弃
            title_zh = cand
    topics: list[str] = []
    m = re.search(r"主题[：:]\s*(.+)", reply)
    if m:
        topics = [t.strip() for t in re.split(r"[、,，;；/]", m.group(1))
                  if t.strip() and "<" not in t and ">" not in t][:5]
    return title_zh, topics


def enrich_task(task_id: int) -> None:
    """为已完成任务补齐标题与主题词（幂等：已有标题且无配置档时直接返回）。"""
    task_dir = config.TASKS_DIR / str(task_id)
    with Session(engine) as s:
        task = s.get(Task, task_id)
        if not task or task.status != "done":
            return
        profile = s.get(Profile, task.profile_id)
        title_en = _tex_title(task_dir / "arxiv_src")
        title_zh = _tex_title(task_dir / "arxiv_out")
        if title_zh == title_en:  # 译文标题没变（引擎回退等）就不算中文标题
            title_zh = None
        if not title_en:
            title_en = Path(task.filename).stem

        topics: list[str] = []
        if profile:
            try:
                from app.services.chat import chat_reply
                from app.services.paragraphs import context_text

                ctx = context_text(task_dir, 4000)
                reply = chat_reply(profile, ctx, [{"role": "user", "content": (
                    "请阅读以上论文节选，只输出两行（不要输出其他内容）：\n"
                    "中文标题：<论文标题的中文翻译>\n"
                    "主题：<3~5 个中文主题词，用顿号分隔>")}])
                llm_title, topics = parse_enrich_reply(reply)
                if not title_zh and llm_title:
                    title_zh = llm_title
            except Exception:  # noqa: BLE001 - LLM 失败不影响标题落库
                logger.info("任务 %s 主题词生成失败", task_id, exc_info=True)

        task.title_en = title_en
        task.title_zh = title_zh
        task.topics_json = json.dumps(topics, ensure_ascii=False) if topics else None
        s.add(task)
        s.commit()


def enrich_task_async(task_id: int) -> None:
    threading.Thread(target=enrich_task, args=(task_id,), daemon=True).start()


def backfill() -> None:
    """启动时回填：历史已完成任务缺标题的逐个补齐（后台线程）。"""
    with Session(engine) as s:
        ids = [t.id for t in s.exec(select(Task).where(Task.status == "done"))
               if not getattr(t, "title_en", None)]
    for task_id in ids:
        try:
            enrich_task(task_id)
        except Exception:  # noqa: BLE001
            logger.warning("回填任务 %s 元数据失败", task_id, exc_info=True)


def backfill_async() -> None:
    threading.Thread(target=backfill, daemon=True).start()
