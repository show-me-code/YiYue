"""任务级翻译缓存：文件哈希 + 规范化选项哈希 → 已完成任务 id。

索引存于 data/cache-index.json；输出文件仍在各自任务目录，命中时
TaskOutput 行直接引用其绝对路径，不复制文件。
"""

import hashlib
import json
import logging
import os
import threading
import uuid
from pathlib import Path

from app import config

logger = logging.getLogger(__name__)

INDEX_PATH = config.DATA_DIR / "cache-index.json"

# 翻译/排版管线版本：修改预处理、提示词、宏包注入等会影响产出的逻辑时递增，
# 使旧任务的缓存键失效——否则"修好了却仍命中旧产出"，改动等于没生效。
PIPELINE_VERSION = "2026-10-09.1"

# 索引是"读-改-写"的 JSON 文件，且 FastAPI 会在线程池与事件循环里同时碰它；
# 没有锁会丢更新（两个任务同时登记，只剩一个）。同进程一把互斥锁就够。
_INDEX_LOCK = threading.RLock()


def compute_cache_key(file_hash: str, options: dict) -> str:
    canonical = json.dumps(options, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((file_hash + "\n" + canonical).encode("utf-8")).hexdigest()


def _load_index() -> dict[str, int]:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("缓存索引损坏，按空索引处理：%s", INDEX_PATH, exc_info=True)
        return {}


def _save_index(index: dict[str, int]) -> None:
    """原子写：临时文件名带唯一后缀，避免并发写互相覆盖。"""
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_name(f"{INDEX_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(INDEX_PATH)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def find_cached_task(cache_key: str) -> int | None:
    with _INDEX_LOCK:
        return _load_index().get(cache_key)


def register_cache(cache_key: str, task_id: int) -> bool:
    """登记缓存键；失败只记警告并返回 False。

    调用点在任务已经产出成品**之后**：这里是"锦上添花"，抛异常会让一次
    成功的翻译被编排器判成 failed（用户看到失败但其实 PDF 已经好了）。
    """
    try:
        with _INDEX_LOCK:
            index = _load_index()
            index[cache_key] = task_id
            _save_index(index)
        return True
    except OSError:
        logger.warning("写入缓存索引失败（忽略，不影响本次产出）", exc_info=True)
        return False


def remove_task_from_cache(task_id: int) -> None:
    try:
        with _INDEX_LOCK:
            index = _load_index()
            index = {k: v for k, v in index.items() if v != task_id}
            _save_index(index)
    except OSError:
        logger.warning("更新缓存索引失败（忽略）", exc_info=True)


def glossary_hash(entries: list[dict]) -> str:
    canonical = json.dumps(
        sorted((e["source"], e["target"], e["tgt_lang"]) for e in entries),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
