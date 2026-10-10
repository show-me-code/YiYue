"""逐段对照阅读 + AI 阅读助手 API。"""

import asyncio
import hashlib
import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from app import config
from app.db import get_session
from app.models import Profile, Task
from app.schemas import ChatIn
from app.services import paragraphs
from app.services.chat import ChatError, chat_reply

router = APIRouter(prefix="/api/tasks", tags=["reader"])

_MAX_MESSAGES = 40
_MAX_CONTENT_CHARS = 8000


@router.get("/{task_id}/paragraphs")
async def get_paragraphs(task_id: int, s: Session = Depends(get_session)):
    """段落对齐结果：原文段落 + 紧跟其后的译文段落。"""
    if not s.get(Task, task_id):
        raise HTTPException(404, "任务不存在")
    return await asyncio.to_thread(
        paragraphs.load_pairs, config.TASKS_DIR / str(task_id))


_IMG_SAFE = re.compile(r"^[A-Za-z0-9_./-]+$")


@router.get("/{task_id}/figimage")
async def fig_image(task_id: int, path: str, s: Session = Depends(get_session)):
    """逐段对照里的插图：位图直接返回，PDF 矢量图渲染成 PNG（带缓存）。"""
    if not s.get(Task, task_id):
        raise HTTPException(404, "任务不存在")
    if not path or ".." in path or not _IMG_SAFE.match(path):
        raise HTTPException(400, "非法图片路径")
    task_dir = config.TASKS_DIR / str(task_id)
    root = task_dir / "arxiv_out"
    if not root.is_dir():
        root = task_dir / "arxiv_src"
    file = (root / path).resolve()
    if not str(file).startswith(str(root.resolve()) + os.sep) or not file.is_file():
        raise HTTPException(404, "图片不存在")

    if file.suffix.lower() in (".png", ".jpg", ".jpeg"):
        return FileResponse(file)
    if file.suffix.lower() != ".pdf":
        raise HTTPException(400, "不支持的图片格式")

    cache_dir = task_dir / "figcache"
    cache_dir.mkdir(exist_ok=True)
    cached = cache_dir / (hashlib.sha1(path.encode()).hexdigest() + ".png")
    if not cached.exists():
        import pymupdf

        def _render() -> None:
            with pymupdf.open(file) as doc:
                pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(2, 2),
                                        alpha=False)
                pix.save(cached)
        try:
            await asyncio.to_thread(_render)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"图片渲染失败：{e}") from e
    return FileResponse(cached)


# 历史保存的口径比问答宽松得多：长回答（总结类可达上万字）也要能存下来
_MAX_HISTORY_MESSAGES = 200
_MAX_HISTORY_CONTENT_CHARS = 50000


def _validate_messages(messages: list[dict], allow_empty: bool = False,
                       max_messages: int = _MAX_MESSAGES,
                       max_content: int = _MAX_CONTENT_CHARS) -> None:
    if not allow_empty and not messages:
        raise HTTPException(400, "消息列表为空或过长")
    if len(messages) > max_messages:
        raise HTTPException(400, "消息列表为空或过长")
    for m in messages:
        if m["role"] not in ("user", "assistant"):
            raise HTTPException(400, "消息 role 只能是 user / assistant")
        if not m["content"].strip() or len(m["content"]) > max_content:
            raise HTTPException(400, "消息内容为空或过长")


@router.get("/{task_id}/chat/history")
def get_chat_history(task_id: int, s: Session = Depends(get_session)):
    """该论文的历史会话（逐篇持久化，重开阅读页时恢复）。"""
    if not s.get(Task, task_id):
        raise HTTPException(404, "任务不存在")
    f = config.TASKS_DIR / str(task_id) / "chat_history.json"
    if not f.exists():
        return {"messages": []}
    try:
        return {"messages": json.loads(f.read_text("utf-8"))}
    except (OSError, json.JSONDecodeError):
        return {"messages": []}


@router.put("/{task_id}/chat/history")
def save_chat_history(task_id: int, body: ChatIn, s: Session = Depends(get_session)):
    """保存该论文的会话记录（整体覆盖，由前端在每次问答后提交）。"""
    if not s.get(Task, task_id):
        raise HTTPException(404, "任务不存在")
    messages = [m.model_dump() for m in body.messages]
    _validate_messages(messages, allow_empty=True,
                       max_messages=_MAX_HISTORY_MESSAGES,
                       max_content=_MAX_HISTORY_CONTENT_CHARS)
    f = config.TASKS_DIR / str(task_id) / "chat_history.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(messages, ensure_ascii=False), "utf-8")
    tmp.replace(f)
    return {"ok": True}


@router.post("/{task_id}/chat")
async def chat(task_id: int, body: ChatIn, s: Session = Depends(get_session)):
    """AI 阅读助手：基于论文正文的多轮问答，复用任务所属配置档。"""
    task = s.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    profile = s.get(Profile, task.profile_id)
    if not profile:
        raise HTTPException(400, "任务所属配置档已被删除，无法使用 AI 助手")

    messages = [m.model_dump() for m in body.messages]
    _validate_messages(messages)
    if messages[-1]["role"] != "user":
        raise HTTPException(400, "最后一条消息必须来自用户")

    task_dir = config.TASKS_DIR / str(task_id)
    context = await asyncio.to_thread(paragraphs.context_text, task_dir)
    try:
        reply = await asyncio.to_thread(chat_reply, profile, context, messages)
    except ChatError as e:
        raise HTTPException(502, str(e)) from e
    return {"reply": reply}
