"""任务 API：上传/列表/详情/取消/重试/删除/SSE 进度流。"""

import asyncio
import hashlib
import json
import logging
import shutil

from fastapi import (APIRouter, Depends, File, Form, HTTPException, UploadFile)
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app import config
from app.db import engine, get_session
from app.models import GlossaryEntry, Profile, Task, TaskOutput
from app.schemas import TaskOptionsIn
from app.services import cache, pdfops
from app.services.orchestrator import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def s_get_exists(task_id: int) -> bool:
    with Session(engine) as s:
        return s.get(Task, task_id) is not None


def _task_json(task: Task, outputs: list[TaskOutput] | None = None) -> dict:
    progress = json.loads(task.progress_json) if task.progress_json else {}
    report_path = config.TASKS_DIR / str(task.id) / "verify" / "verify_report.json"
    verification = None
    if report_path.exists():
        try:
            rep = json.loads(report_path.read_text("utf-8"))
            verification = {
                "verdict": rep.get("verdict"),
                "reasons": rep.get("reasons", []),
                "notes": rep.get("notes", []),
                "pages": rep.get("pages"),
                "overflow": rep.get("overflow", {}),
                "content": rep.get("content", {}),
                "url": f"/api/tasks/{task.id}/verify",
            }
        except (OSError, json.JSONDecodeError):
            verification = None
    return {
        "id": task.id,
        "filename": task.filename,
        "status": task.status,
        "error": task.error,
        "page_count": task.page_count,
        "size_bytes": task.size_bytes,
        "lang_in": task.lang_in,
        "lang_out": task.lang_out,
        "pages": task.pages,
        "part_size": task.part_size,
        "scan_mode": task.scan_mode,
        "engine": getattr(task, "engine", "auto"),
        "engine_used": getattr(task, "engine_used", None),
        "arxiv_id": getattr(task, "arxiv_id", None),
        "auto_extract_glossary": task.auto_extract_glossary,
        "cache_hit": task.cache_key is not None and task.status == "done"
                     and task.error == "cache",
        "progress": progress,
        "verification": verification,
        "created_at": str(task.created_at),
        "finished_at": str(task.finished_at) if task.finished_at else None,
        "outputs": [
            {"kind": o.kind, "url": f"/api/tasks/{task.id}/files/{o.kind}"}
            for o in (outputs or [])
        ],
    }


def _glossary_snapshot(s: Session, lang_out: str) -> tuple[list[dict], str]:
    """返回 (启用的适用条目快照, 其内容哈希)。"""
    entries = s.exec(select(GlossaryEntry).where(GlossaryEntry.enabled == True)).all()  # noqa: E712
    applicable = [
        {"source": e.source, "target": e.target, "tgt_lang": e.tgt_lang,
         "enabled": True}
        for e in entries if not e.tgt_lang or e.tgt_lang == lang_out
    ]
    return applicable, cache.glossary_hash(applicable)


@router.post("")
async def create_task(
    file: UploadFile | None = File(None),
    profile_id: int = Form(...),
    lang_in: str = Form("en"),
    lang_out: str = Form("zh"),
    pages: str = Form(""),
    part_size: int = Form(20),
    scan_mode: str = Form("off"),
    auto_extract_glossary: bool = Form(False),
    use_glossary: bool = Form(True),
    engine: str = Form("auto"),
    arxiv_id: str = Form(""),
    s: Session = Depends(get_session),
):
    opts = TaskOptionsIn(
        profile_id=profile_id, lang_in=lang_in, lang_out=lang_out,
        pages=pages or None, part_size=part_size, scan_mode=scan_mode,
        auto_extract_glossary=auto_extract_glossary, use_glossary=use_glossary,
        engine=engine or "auto", arxiv_id=arxiv_id or None,
    )
    if opts.scan_mode not in ("off", "workaround", "auto"):
        raise HTTPException(400, "scan_mode 无效")
    if opts.engine not in ("auto", "arxiv", "babeldoc"):
        raise HTTPException(400, "engine 无效")
    if not s.get(Profile, opts.profile_id):
        raise HTTPException(400, "配置档不存在")

    # ---- 文件：上传的 PDF，或按 arXiv ID 下载 ----
    has_file = file is not None and bool(file.filename)
    data: bytes | None = None
    upload_name: str | None = None
    if has_file:
        data = await file.read()
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(400, f"文件超过 {config.MAX_UPLOAD_MB}MB 上限")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, "仅支持 PDF 文件")
        upload_name = file.filename

    snapshot, ghash = _glossary_snapshot(s, opts.lang_out) \
        if opts.use_glossary else ([], cache.glossary_hash([]))

    # ---- 解析 arXiv ID：用户输入 > 文件名 > PDF 首页文本 ----
    from app.services.arxiv_engine import ArxivEngineError, detect_arxiv_id

    resolved_arxiv_id = None
    if opts.engine in ("auto", "arxiv"):
        resolved_arxiv_id = detect_arxiv_id(
            filename=upload_name or "", user_input=opts.arxiv_id or ""
        )
        if not resolved_arxiv_id and data:
            import os as _os
            import tempfile

            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tmp.write(data)
            tmp.close()
            try:
                resolved_arxiv_id = detect_arxiv_id(pdf_path=tmp.name)
            finally:
                _os.unlink(tmp.name)

    if data is None:
        # 仅提供 ID 的模式：必须有可解析的 ID，且引擎允许走 arXiv 路线
        if opts.engine == "babeldoc":
            raise HTTPException(400, "仅提供 arXiv ID 时，引擎需为「自动」或「arXiv 直译」")
        if not resolved_arxiv_id:
            raise HTTPException(400, "请上传 PDF 文件，或填写有效的 arXiv ID")
        from app.services.arxiv_engine import download_pdf

        try:
            data = await asyncio.to_thread(download_pdf, resolved_arxiv_id)
        except ArxivEngineError as e:
            raise HTTPException(400, str(e)) from e

    assert data is not None
    file_hash = hashlib.sha256(data).hexdigest()

    cache_options = {
        "lang_in": opts.lang_in, "lang_out": opts.lang_out,
        "pages": opts.pages or "", "part_size": opts.part_size,
        "scan_mode": opts.scan_mode,
        "auto_extract_glossary": opts.auto_extract_glossary,
        "glossary": ghash,
        # 缓存键按"解析后实际将执行的引擎"区分，避免回退结果污染 arXiv 键
        "engine": "arxiv" if (opts.engine in ("auto", "arxiv")
                              and resolved_arxiv_id) else "babeldoc",
        "arxiv_id": resolved_arxiv_id or "",
        "pipeline_version": cache.PIPELINE_VERSION,
    }
    cache_key = cache.compute_cache_key(file_hash, cache_options)

    default_name = f"arXiv-{resolved_arxiv_id}.pdf" if resolved_arxiv_id \
        else "document.pdf"
    task = Task(
        filename=upload_name or default_name,
        file_hash=file_hash, size_bytes=len(data),
        profile_id=opts.profile_id, lang_in=opts.lang_in,
        lang_out=opts.lang_out, pages=opts.pages, part_size=opts.part_size,
        scan_mode=opts.scan_mode,
        engine=opts.engine,
        arxiv_id=resolved_arxiv_id,
        auto_extract_glossary=opts.auto_extract_glossary,
        glossary_snapshot_json=json.dumps(snapshot, ensure_ascii=False)
        if snapshot else None,
        cache_key=cache_key,
    )
    s.add(task)
    s.commit()
    s.refresh(task)

    task_dir = config.TASKS_DIR / str(task.id)
    task_dir.mkdir(parents=True, exist_ok=True)
    original = task_dir / "original.pdf"
    original.write_bytes(data)
    task.page_count = pdfops.page_count(original)
    s.add(task)
    s.commit()

    # 任务级缓存命中：直接引用历史输出
    cached_id = cache.find_cached_task(cache_key)
    if cached_id and cached_id != task.id:
        prev_outputs = s.exec(
            select(TaskOutput).where(TaskOutput.task_id == cached_id)
        ).all()
        if prev_outputs:
            for o in prev_outputs:
                s.add(TaskOutput(task_id=task.id, kind=o.kind, path=o.path))
            task.status = "done"
            task.error = "cache"
            task.finished_at = task.created_at
            task.progress_json = json.dumps({
                "stage": "缓存命中", "overall_progress": 100,
                "pages_done": task.page_count,
            })
            s.add(task)
            s.commit()
            s.refresh(task)
            outs = s.exec(
                select(TaskOutput).where(TaskOutput.task_id == task.id)
            ).all()
            return _task_json(task, outs)

    manager.submit(task.id)
    return _task_json(task)


@router.get("")
def list_tasks(s: Session = Depends(get_session)):
    tasks = s.exec(select(Task).order_by(Task.id.desc())).all()
    grouped: dict[int, list[TaskOutput]] = {}
    for o in s.exec(select(TaskOutput)).all():
        grouped.setdefault(o.task_id, []).append(o)
    return [_task_json(t, grouped.get(t.id, [])) for t in tasks]


@router.get("/{task_id}")
def get_task(task_id: int, s: Session = Depends(get_session)):
    task = s.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    outputs = s.exec(select(TaskOutput).where(TaskOutput.task_id == task_id)).all()
    return _task_json(task, outputs)


@router.get("/{task_id}/verify")
def get_task_verification(task_id: int, s: Session = Depends(get_session)):
    """闭环版式验证报告（用户规格第 5 步）：逐页版心偏差、告警对比、结论。"""
    if not s.get(Task, task_id):
        raise HTTPException(404, "任务不存在")
    report = config.TASKS_DIR / str(task_id) / "verify" / "verify_report.json"
    if not report.exists():
        raise HTTPException(404, "该任务没有验证报告（仅 arXiv 源码直译引擎产出）")
    return json.loads(report.read_text("utf-8"))


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, s: Session = Depends(get_session)):
    task = s.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status in ("queued", "translating", "merging"):
        await manager.cancel(task_id)
    return {"ok": True}


@router.post("/{task_id}/retry")
def retry_task(task_id: int, s: Session = Depends(get_session)):
    task = s.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status not in ("failed", "cancelled", "done"):
        raise HTTPException(400, "当前状态不可重试")
    # 清理旧输出与中间文件（保留 original.pdf）
    for o in s.exec(select(TaskOutput).where(TaskOutput.task_id == task_id)):
        s.delete(o)
    s.commit()
    task.status = "queued"
    task.error = None
    task.progress_json = None
    task.finished_at = None
    s.add(task)
    s.commit()
    s.refresh(task)
    shutil.rmtree(config.TASKS_DIR / str(task_id) / "work", ignore_errors=True)
    (config.TASKS_DIR / str(task_id) / "mono.pdf").unlink(missing_ok=True)
    (config.TASKS_DIR / str(task_id) / "dual.pdf").unlink(missing_ok=True)
    manager.submit(task_id)
    return _task_json(task)


@router.delete("/{task_id}")
async def delete_task(task_id: int, s: Session = Depends(get_session)):
    task = s.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status in ("translating", "merging"):
        await manager.cancel(task_id)
    for o in s.exec(select(TaskOutput).where(TaskOutput.task_id == task_id)):
        s.delete(o)
    s.delete(task)
    s.commit()
    manager.remove_task_files(task_id)
    return {"ok": True}


@router.get("/{task_id}/events")
async def task_events(task_id: int):
    if not s_get_exists(task_id):
        raise HTTPException(404, "任务不存在")

    async def gen():
        q = manager.subscribe(task_id)
        try:
            with Session(engine) as s:
                t = s.get(Task, task_id)
                if t:
                    outputs = s.exec(
                        select(TaskOutput).where(TaskOutput.task_id == task_id)
                    ).all()
                    yield f"data: {json.dumps({'type': 'state', 'task': _task_json(t, outputs)}, ensure_ascii=False, default=str)}\n\n"
            terminal = {"done", "error", "cancelled"}
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {msg}\n\n"
                try:
                    if json.loads(msg).get("type") in terminal:
                        break
                except json.JSONDecodeError:
                    continue
        finally:
            manager.unsubscribe(task_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
