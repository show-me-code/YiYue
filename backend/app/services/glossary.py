"""术语表服务：数据库条目 → BabelDOC Glossary。"""

from babeldoc.glossary import Glossary as BabelGlossary
from babeldoc.glossary import GlossaryEntry as BabelGlossaryEntry

from app.models import GlossaryEntry


def entries_to_glossaries(
    entries: list[GlossaryEntry], tgt_lang: str
) -> list[BabelGlossary]:
    """按目标语言过滤启用的条目；无有效条目时返回空列表。

    条目可能是 ORM 行，也可能是从任务快照 JSON 还原的对象（快照早期版本不带
    enabled 字段），因此用 getattr 兜底——否则一旦用户建了术语表，任务必然报
    "'SimpleNamespace' object has no attribute 'enabled'"。
    """
    valid = [
        BabelGlossaryEntry(
            source=e.source.strip(),
            target=e.target.strip(),
            target_language=(e.tgt_lang or None),
        )
        for e in entries
        if getattr(e, "enabled", True) and e.source.strip() and e.target.strip()
        and (not e.tgt_lang or e.tgt_lang == tgt_lang)
    ]
    if not valid:
        return []
    return [BabelGlossary(name="user-glossary", entries=valid)]
