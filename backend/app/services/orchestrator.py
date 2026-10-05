"""任务编排器：串行队列 + 状态机 + 分段翻译与增量合并 + SSE 广播。

SSE 事件（JSON 字符串入订阅队列）：
  state   {type, task:{...}}          连接建立/状态变更快照
  progress {type, part_index, part_total, stage, stage_progress, overall_progress, pages_done}
  pages_available {type, pages_done}
  done {type, outputs:[{kind,path}]}
  error {type, message}
  cancelled {type}
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import Profile, Task, TaskOutput
from app.services import babeldoc_runner, cache, pdfops
from app.services.babeldoc_runner import BabelDOCError, RunnerOptions
from app.services.glossary import entries_to_glossaries

logger = logging.getLogger(__name__)


class TaskCancelled(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


class TaskManager:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._worker_loop_owner: asyncio.AbstractEventLoop | None = None
        self._inner: asyncio.Task | None = None  # 当前任务的运行 task
        self.current_task_id: int | None = None
        self._cancel_requested: set[int] = set()
        self._subscribers: dict[int, set[asyncio.Queue]] = {}

    # ---------- 订阅 ----------
    def subscribe(self, task_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: int, q: asyncio.Queue) -> None:
        self._subscribers.get(task_id, set()).discard(q)

    def broadcast(self, task_id: int, payload: dict) -> None:
        msg = json.dumps(payload, ensure_ascii=False, default=str)
        for q in list(self._subscribers.get(task_id, ())):
            q.put_nowait(msg)

    # ---------- 队列 ----------
    def submit(self, task_id: int) -> None:
        self.queue.put_nowait(task_id)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if (self._worker is not None and not self._worker.done()
                and self._worker_loop_owner is loop):
            return
        # 事件循环变更（如测试环境中 TestClient 反复启停）时重建 worker。
        # asyncio.Queue 绑定循环，一并重建；旧循环中的排队任务视为丢失。
        self.queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._worker_loop(), name="task-worker")
        self._worker_loop_owner = loop

    async def cancel(self, task_id: int) -> bool:
        """取消任务：排队中会在出队时标记；翻译中取消当前运行。"""
        self._cancel_requested.add(task_id)
        if task_id == self.current_task_id and self._inner and not self._inner.done():
            self._inner.cancel()
        return True

    # ---------- 状态写回 ----------
    def _update_task(self, task_id: int, **fields) -> Task | None:
        with Session(engine) as s:
            task = s.get(Task, task_id)
            if task is None:
                return None
            for k, v in fields.items():
                setattr(task, k, v)
            s.add(task)
            s.commit()
            s.refresh(task)
            return task

    def _snapshot(self, task: Task) -> dict:
        progress = json.loads(task.progress_json) if task.progress_json else {}
        return {
            "id": task.id,
            "filename": task.filename,
            "status": task.status,
            "error": task.error,
            "page_count": task.page_count,
            "progress": progress,
            "lang_in": task.lang_in,
            "lang_out": task.lang_out,
            "pages_done": progress.get("pages_done", 0),
        }

    def _set_status(self, task_id: int, status: str, **fields) -> Task | None:
        task = self._update_task(task_id, status=status, **fields)
        if task:
            self.broadcast(task_id, {"type": "state", "task": self._snapshot(task)})
        return task

    def _set_progress(self, task_id: int, **progress) -> None:
        task = self._update_task(task_id, progress_json=json.dumps(progress))
        if task:
            self.broadcast(task_id, {"type": "progress", **progress})

    def _current_progress(self, task_id: int) -> dict:
        with Session(engine) as s:
            t = s.get(Task, task_id)
            return json.loads(t.progress_json) if t and t.progress_json else {}

    # ---------- 主循环 ----------
    async def _worker_loop(self) -> None:
        while True:
            task_id = await self.queue.get()
            self.current_task_id = task_id
            inner = asyncio.create_task(self._run_task(task_id))
            self._inner = inner
            await asyncio.wait({inner})
            if inner.cancelled():
                # 未走 TaskCancelled 路径的直接取消（如段间等待时被取消）
                self._set_status(task_id, "cancelled", finished_at=_now())
                self.broadcast(task_id, {"type": "cancelled"})
            else:
                exc = inner.exception()
                if exc is not None:
                    logger.exception("task %s failed", task_id)
                    self._set_status(
                        task_id, "failed", error=str(exc), finished_at=_now()
                    )
                    self.broadcast(task_id, {"type": "error", "message": str(exc)})
            self._inner = None
            self.current_task_id = None
            self._cancel_requested.discard(task_id)

    async def _run_task(self, task_id: int) -> None:
        with Session(engine) as s:
            task = s.get(Task, task_id)
            profile = s.get(Profile, task.profile_id) if task else None
        if task is None:
            return
        if task_id in self._cancel_requested:
            raise TaskCancelled()
        if profile is None:
            self._set_status(task_id, "failed", error="配置档不存在", finished_at=_now())
            return

        task_dir = config.TASKS_DIR / str(task_id)
        original = task_dir / "original.pdf"
        work_dir = task_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        self._set_status(task_id, "translating", started_at=_now(), error=None)
        self._set_progress(
            task_id, part_index=0, part_total=0, stage="准备中",
            stage_progress=0, overall_progress=0, pages_done=0,
        )

        # 页码范围：先抽取所选页作为工作文件
        source = original
        if task.pages:
            source = work_dir / "selected.pdf"
            await asyncio.to_thread(self._extract_pages, original, source, task.pages)

        n_pages = await asyncio.to_thread(pdfops.page_count, source)

        if task.part_size > 0 and n_pages > task.part_size:
            part_paths = await asyncio.to_thread(
                pdfops.split_pdf, source, work_dir, task.part_size
            )
        else:
            part_paths = [source]

        part_total = len(part_paths)
        part_page_counts = [pdfops.page_count(p) for p in part_paths]

        mono_parts: list = []
        dual_parts: list = []
        pages_done = 0

        try:
            for i, part_path in enumerate(part_paths):
                if task_id in self._cancel_requested:
                    raise TaskCancelled()
                self._set_progress(
                    task_id, part_index=i, part_total=part_total, stage="解析排版",
                    stage_progress=0,
                    overall_progress=round(100 * pages_done / n_pages, 1),
                    pages_done=pages_done,
                )
                result = await self._translate_part_with_retry(
                    task, profile, part_path, work_dir / f"out_{i:04d}"
                )
                if result.mono_pdf_path is None:
                    raise BabelDOCError("翻译结果缺少单语 PDF")
                mono_parts.append(result.mono_pdf_path)
                if result.dual_pdf_path:
                    dual_parts.append(result.dual_pdf_path)

                # 增量合并：翻译完一段立刻可读
                pages_done += part_page_counts[i]
                self._set_status(task_id, "merging")
                await asyncio.to_thread(
                    pdfops.merge_pdfs, mono_parts, task_dir / "mono.pdf"
                )
                if dual_parts:
                    await asyncio.to_thread(
                        pdfops.merge_pdfs, dual_parts, task_dir / "dual.pdf"
                    )
                self.broadcast(
                    task_id, {"type": "pages_available", "pages_done": pages_done}
                )
                self._set_status(task_id, "translating")
        except TaskCancelled:
            self._set_status(task_id, "cancelled", finished_at=_now())
            self.broadcast(task_id, {"type": "cancelled"})
            return

        # 完成：登记输出
        self._set_status(task_id, "merging")
        outputs = [
            {"kind": "original", "path": str(original)},
            {"kind": "mono", "path": str(task_dir / "mono.pdf")},
        ]
        if (task_dir / "dual.pdf").exists():
            outputs.append({"kind": "dual", "path": str(task_dir / "dual.pdf")})
        with Session(engine) as s:
            for row in s.exec(select(TaskOutput).where(TaskOutput.task_id == task_id)):
                s.delete(row)
            for o in outputs:
                s.add(TaskOutput(task_id=task_id, kind=o["kind"], path=o["path"]))
            s.commit()
        self._set_progress(
            task_id, part_index=part_total, part_total=part_total, stage="完成",
            stage_progress=100, overall_progress=100, pages_done=n_pages,
        )
        self._set_status(task_id, "done", finished_at=_now(), error=None)
        if task.cache_key:
            cache.register_cache(task.cache_key, task_id)
        self.broadcast(task_id, {"type": "done", "outputs": outputs})

    # ---------- 段翻译 ----------
    async def _translate_part_with_retry(self, task: Task, profile: Profile,
                                         part_path, out_dir):
        last_err: Exception | None = None
        for attempt in range(config.PART_RETRY_TIMES + 1):
            try:
                return await self._translate_part(task, profile, part_path, out_dir)
            except TaskCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("part %s attempt %s failed: %s",
                               part_path, attempt + 1, e)
                if attempt < config.PART_RETRY_TIMES:
                    await asyncio.sleep(config.RETRY_BACKOFF_SECONDS[min(attempt, 1)])
        raise BabelDOCError(f"翻译失败：{last_err}")

    async def _translate_part(self, task: Task, profile: Profile,
                              part_path, out_dir):
        glossaries = []
        if task.glossary_snapshot_json:
            entries = json.loads(task.glossary_snapshot_json)
            glossaries = entries_to_glossaries(
                [SimpleNamespace(**e) for e in entries], task.lang_out
            )
        opts = RunnerOptions(
            input_file=part_path,
            output_dir=out_dir,
            lang_in=task.lang_in,
            lang_out=task.lang_out,
            scan_mode=task.scan_mode,
            glossaries=glossaries,
            auto_extract_glossary=task.auto_extract_glossary,
        )
        config_obj = await asyncio.to_thread(
            babeldoc_runner.build_translation_config, profile, opts
        )
        result = None
        async for ev in babeldoc_runner.translate_stream(config_obj):
            if ev["type"] == "progress":
                self._report_stage_progress(task.id, ev)
            elif ev["type"] == "finish":
                result = ev["result"]
        if result is None:
            if task.id in self._cancel_requested:
                raise TaskCancelled()
            raise BabelDOCError("翻译流程未返回结果")
        return result

    def _report_stage_progress(self, task_id: int, ev: dict) -> None:
        prog = self._current_progress(task_id)
        part_total = prog.get("part_total") or 1
        part_index = prog.get("part_index") or 0
        pages_done = prog.get("pages_done", 0)
        part_frac = ev["overall_progress"] / 100.0
        overall = round(100 * (part_index + part_frac) / part_total, 1)
        self._set_progress(
            task_id,
            part_index=part_index,
            part_total=part_total,
            stage=ev["stage"],
            stage_progress=ev["stage_progress"],
            overall_progress=overall,
            pages_done=pages_done,
        )

    # ---------- 其它 ----------
    @staticmethod
    def _extract_pages(src, dst, pages_expr: str) -> None:
        ranges = pdfops.parse_page_ranges(pages_expr)
        reader = PdfReader(src)
        writer = PdfWriter()
        total = len(reader.pages)
        for a, b in ranges:
            for p0 in range(max(a, 1), min(b, total) + 1):
                writer.add_page(reader.pages[p0 - 1])
        with open(dst, "wb") as f:
            writer.write(f)

    def remove_task_files(self, task_id: int) -> None:
        shutil.rmtree(config.TASKS_DIR / str(task_id), ignore_errors=True)
        cache.remove_task_from_cache(task_id)


manager = TaskManager()


def recover_interrupted() -> None:
    """应用重启后，把中断的任务标记为失败以便重试。"""
    with Session(engine) as s:
        rows = s.exec(
            select(Task).where(Task.status.in_(["queued", "translating", "merging"]))
        ).all()
        for t in rows:
            t.status = "failed"
            t.error = "应用重启导致任务中断，请重试"
            t.finished_at = _now()
            s.add(t)
        s.commit()
