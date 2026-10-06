"""任务级翻译缓存：文件哈希 + 规范化选项哈希 → 已完成任务 id。

索引存于 data/cache-index.json；输出文件仍在各自任务目录，命中时
TaskOutput 行直接引用其绝对路径，不复制文件。
"""

import hashlib
import json
from pathlib import Path

from app import config

INDEX_PATH = config.DATA_DIR / "cache-index.json"

# 翻译/排版管线版本：修改预处理、提示词、宏包注入等会影响产出的逻辑时递增，
# 使旧任务的缓存键失效——否则"修好了却仍命中旧产出"，改动等于没生效。
PIPELINE_VERSION = "2026-10-06.7"


def compute_cache_key(file_hash: str, options: dict) -> str:
    canonical = json.dumps(options, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((file_hash + "\n" + canonical).encode("utf-8")).hexdigest()


def _load_index() -> dict[str, int]:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_index(index: dict[str, int]) -> None:
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(INDEX_PATH)


def find_cached_task(cache_key: str) -> int | None:
    return _load_index().get(cache_key)


def register_cache(cache_key: str, task_id: int) -> None:
    index = _load_index()
    index[cache_key] = task_id
    _save_index(index)


def remove_task_from_cache(task_id: int) -> None:
    index = _load_index()
    index = {k: v for k, v in index.items() if v != task_id}
    _save_index(index)


def glossary_hash(entries: list[dict]) -> str:
    canonical = json.dumps(
        sorted((e["source"], e["target"], e["tgt_lang"]) for e in entries),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
