"""SQLModel 数据表定义。"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Profile(SQLModel, table=True):
    """OpenAI 兼容 API 配置档。"""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    base_url: str = ""  # 留空则用 openai 官方默认
    api_key: str = ""
    model: str
    qps: int = 4
    pool_max_workers: int | None = None
    custom_system_prompt: str | None = None
    is_default: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class GlossaryEntry(SQLModel, table=True):
    """用户术语表条目。"""

    id: int | None = Field(default=None, primary_key=True)
    source: str
    target: str
    tgt_lang: str = "zh"
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Task(SQLModel, table=True):
    """一次翻译任务。"""

    id: int | None = Field(default=None, primary_key=True)
    filename: str
    file_hash: str
    size_bytes: int
    page_count: int = 0
    status: str = "queued"  # queued|translating|merging|done|failed|cancelled
    error: str | None = None
    profile_id: int
    lang_in: str = "en"
    lang_out: str = "zh"
    pages: str | None = None
    part_size: int = 20  # 0 表示整本翻译
    scan_mode: str = "off"  # off|workaround|auto
    auto_extract_glossary: bool = False
    glossary_snapshot_json: str | None = None  # 术语快照，保证可复现
    cache_key: str | None = None
    progress_json: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskOutput(SQLModel, table=True):
    """任务输出文件。path 为绝对路径（缓存命中时会指向别的任务目录）。"""

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    kind: str  # original|mono|dual
    path: str
