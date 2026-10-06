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
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import Profile, Task, TaskOutput
from app.services import arxiv_engine, babeldoc_runner, cache, pdfops, tectonic
from app.services import tex_translate
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
        self._notes: dict[int, str] = {}  # 引擎回退等备注，完成时随任务展示

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
            self._notes.pop(task_id, None)

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

        self._set_status(task_id, "translating", started_at=_now(),
                         error=self._notes.get(task_id))
        self._set_progress(
            task_id, part_index=0, part_total=0, stage="检查翻译服务",
            stage_progress=0, overall_progress=0, pages_done=0,
        )

        # 预检：一次真实调用确认翻译服务可用。
        # 没有这一步，LLM 调不通时引擎会把每段静默回退成原文，
        # 任务“完成”却输出未翻译的 PDF。
        translator = babeldoc_runner.build_translator(
            profile, task.lang_in, task.lang_out
        )
        await asyncio.to_thread(babeldoc_runner.preflight, translator)

        # 引擎路由：auto/arxiv 且检测到 arXiv ID → 走源码直译重编译；
        # 任何失败自动回退下方 BabelDOC 原位排版路径
        engine_kind = (task.engine or "auto")
        arxiv_id = task.arxiv_id
        if engine_kind in ("auto", "arxiv") and not arxiv_id:
            arxiv_id = await asyncio.to_thread(
                arxiv_engine.detect_arxiv_id, task.filename, str(original), None
            )
        if engine_kind in ("auto", "arxiv") and arxiv_id:
            try:
                await self._run_arxiv_task(task, profile, original, arxiv_id)
                return
            except TaskCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                note = f"arXiv 引擎失败（{str(e)[:160]}），已自动回退原位排版引擎。"
                logger.warning("task %s arxiv engine failed: %s", task_id, e)
                self._notes[task_id] = note
                # 缓存键改按实际执行的 babeldoc 引擎登记，避免污染 arXiv 键
                task.cache_key = self._babeldoc_cache_key(task)
                self._update_task(task_id, cache_key=task.cache_key)
                self._set_progress(
                    task_id, part_index=0, part_total=1,
                    stage="已回退原位排版引擎", stage_progress=0,
                    overall_progress=0, pages_done=0,
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
        self._set_status(task_id, "done", finished_at=_now(),
                         error=self._notes.get(task_id))
        if task.cache_key:
            cache.register_cache(task.cache_key, task_id)
        self.broadcast(task_id, {"type": "done", "outputs": outputs})

    def _babeldoc_cache_key(self, task: Task) -> str:
        """回退运行按 babeldoc 引擎计算缓存键（与 arXiv 键隔离）。"""
        options = {
            "lang_in": task.lang_in, "lang_out": task.lang_out,
            "pages": task.pages or "", "part_size": task.part_size,
            "scan_mode": task.scan_mode,
            "auto_extract_glossary": task.auto_extract_glossary,
            "glossary": cache.glossary_hash(
                json.loads(task.glossary_snapshot_json))
            if task.glossary_snapshot_json else cache.glossary_hash([]),
            "engine": "babeldoc", "arxiv_id": task.arxiv_id or "",
        }
        return cache.compute_cache_key(task.file_hash, options)

    # ---------- arXiv 源码直译引擎 ----------
    async def _run_arxiv_task(self, task: Task, profile: Profile,
                              original, arxiv_id: str) -> None:
        """下载 LaTeX 源码 → 逐文件翻译（公式/引用保护）→ Tectonic 原模板重编译。"""
        task_id = task.id
        task_dir = config.TASKS_DIR / str(task_id)

        self._set_progress(
            task_id, part_index=0, part_total=1,
            stage=f"下载 arXiv 源码（{arxiv_id}）", stage_progress=0,
            overall_progress=2, pages_done=0,
        )
        src_dir = task_dir / "arxiv_src"
        src_file = await asyncio.to_thread(
            arxiv_engine.download_source, arxiv_id, src_dir
        )
        texs = await asyncio.to_thread(arxiv_engine.extract_source, src_file, src_dir)
        candidates = await asyncio.to_thread(arxiv_engine.rank_main_tex, texs)
        if not candidates:
            raise arxiv_engine.ArxivEngineError("未找到主 tex 文件")
        main_tex = candidates[0]
        files = await asyncio.to_thread(
            arxiv_engine.collect_doc_files, main_tex, texs
        )
        logger.info("task %s arxiv %s: main=%s files=%s",
                    task_id, arxiv_id, main_tex.name, [f.name for f in files])

        if tectonic.tectonic_path() is None:
            self._set_progress(
                task_id, part_index=0, part_total=1,
                stage="下载 Tectonic 编译器（约 21MB，首次运行）",
                stage_progress=0, overall_progress=5, pages_done=0,
            )
            await asyncio.to_thread(tectonic.download_tectonic)

        # 先用未翻译源码验证哪个候选主文件可编译（arXiv 源码常见多个构建变体）
        chosen: Path | None = None
        for cand in candidates[:3]:
            self._set_progress(
                task_id, part_index=0, part_total=1,
                stage=f"试编译候选主文件 {cand.name}",
                stage_progress=0, overall_progress=6, pages_done=0,
            )
            probe_dir = task_dir / "arxiv_probe"

            def _prepare_probe(dir_path: Path = probe_dir) -> Path:
                if dir_path.exists():
                    shutil.rmtree(dir_path)
                shutil.copytree(src_dir, dir_path,
                                ignore=shutil.ignore_patterns("source.bin"))
                probe_main = dir_path / cand.relative_to(src_dir)
                # 试编译即应用中文预处理：提前拉取 ctex/Fandol 宏包并暴露相关问题
                text = probe_main.read_text("utf-8", errors="replace")
                probe_main.write_text(
                    tex_translate.prepare_for_xetex(text, task.lang_out), "utf-8")
                return probe_main

            probe_main = await asyncio.to_thread(_prepare_probe)
            try:
                await asyncio.to_thread(
                    tectonic.compile_tex, probe_main, probe_dir
                )
                chosen = cand
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("candidate %s failed to compile: %s",
                               cand.name, str(e)[:200])
        if chosen is None:
            raise arxiv_engine.ArxivEngineError(
                "所有候选主文件均无法编译（详见日志）"
            )

        # 复制完整源码树到输出目录（图片/样式/参考文献原样保留）
        out_src = task_dir / "arxiv_out"
        if out_src.exists():
            shutil.rmtree(out_src)
        await asyncio.to_thread(
            shutil.copytree, src_dir, out_src,
            ignore=shutil.ignore_patterns("source.bin"),
        )
        # 样式文件硬编码 "Abstract" → "摘要"（用户规格第 3 步）
        await asyncio.to_thread(tex_translate.patch_style_files, out_src)

        translator = babeldoc_runner.build_translator(
            profile, task.lang_in, task.lang_out
        )
        files = await asyncio.to_thread(
            arxiv_engine.collect_doc_files, chosen, texs
        )
        n_files = max(len(files), 1)

        def chunk_cb(file_index: int, file_name: str):
            def cb(stage: str, done: int, total: int) -> None:
                if task.id in self._cancel_requested:
                    raise TaskCancelled()
                overall = 10 + 80 * (file_index + done / max(total, 1)) / n_files
                self._set_progress(
                    task_id, part_index=0, part_total=1,
                    stage=f"{stage}：{file_name}",
                    stage_progress=round(100 * done / max(total, 1)),
                    overall_progress=round(min(overall, 90), 1), pages_done=0,
                )
            return cb

        for fi, f in enumerate(files):
            rel = f.relative_to(src_dir)
            text = await asyncio.to_thread(
                f.read_text, encoding="utf-8", errors="replace"
            )
            translated = await asyncio.to_thread(
                tex_translate.translate_tex, text, translator,
                task.lang_out, chunk_cb(fi, rel.name),
            )
            translated = tex_translate.prepare_for_xetex(translated, task.lang_out)
            target = out_src / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_text, translated, "utf-8")

        self._set_progress(
            task_id, part_index=0, part_total=1, stage="Tectonic 编译",
            stage_progress=0, overall_progress=92, pages_done=0,
        )
        main_out = out_src / chosen.relative_to(src_dir)
        pdf = None
        for repair_round in range(3):
            try:
                pdf = await asyncio.to_thread(
                    tectonic.compile_tex, main_out, task_dir
                )
                break
            except Exception as e:  # noqa: BLE001
                if repair_round == 2:
                    raise
                # 从日志定位报错文件，还原为原文后重试（局部降级优于整体失败）
                log_path = task_dir / "main.log"
                broken = await asyncio.to_thread(
                    tectonic.find_broken_input_file, log_path, out_src
                )
                orig = (src_dir / broken.relative_to(out_src)) if broken else None
                if broken is None or not orig.exists():
                    raise
                logger.warning("compile failed, restoring %s to original: %s",
                               broken.name, str(e)[:160])
                await asyncio.to_thread(shutil.copy, orig, broken)
        if pdf is None:
            raise arxiv_engine.ArxivEngineError("编译失败")
        shutil.move(str(pdf), str(task_dir / "mono.pdf"))

        outputs = [
            {"kind": "original", "path": str(original)},
            {"kind": "mono", "path": str(task_dir / "mono.pdf")},
        ]
        with Session(engine) as s:
            for row in s.exec(select(TaskOutput).where(TaskOutput.task_id == task_id)):
                s.delete(row)
            for o in outputs:
                s.add(TaskOutput(task_id=task_id, kind=o["kind"], path=o["path"]))
            s.commit()
        self._set_progress(
            task_id, part_index=1, part_total=1, stage="完成",
            stage_progress=100, overall_progress=100, pages_done=task.page_count,
        )
        self._set_status(task_id, "done", finished_at=_now(),
                         error=self._notes.get(task_id))
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
