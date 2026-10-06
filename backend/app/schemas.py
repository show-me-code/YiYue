"""API 请求/响应模型。"""

from pydantic import BaseModel


class ProfileIn(BaseModel):
    name: str
    base_url: str = ""
    api_key: str = ""
    model: str
    protocol: str = "openai"  # openai | anthropic
    qps: int = 4
    pool_max_workers: int | None = None
    custom_system_prompt: str | None = None
    is_default: bool = False


class ProfileOut(BaseModel):
    id: int
    name: str
    base_url: str
    model: str
    protocol: str
    qps: int
    pool_max_workers: int | None
    custom_system_prompt: str | None
    is_default: bool
    api_key_masked: str


class GlossaryIn(BaseModel):
    source: str
    target: str
    tgt_lang: str = "zh"
    enabled: bool = True


class TaskOptionsIn(BaseModel):
    profile_id: int
    lang_in: str = "en"
    lang_out: str = "zh"
    pages: str | None = None
    part_size: int = 20
    scan_mode: str = "off"  # off|workaround|auto
    auto_extract_glossary: bool = False
    use_glossary: bool = True
    engine: str = "auto"  # auto|arxiv|babeldoc
    arxiv_id: str | None = None
