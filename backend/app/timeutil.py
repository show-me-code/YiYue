"""时间序列化：统一成 ISO-8601，并算出任务耗时。

要点：`str(datetime)` 得到的是 "2026-10-07 14:44:34.507355+00:00"（空格分隔、
微秒 6 位）。JS 的 `new Date()` 对这种非标准写法解析结果不确定，前端就不能可靠地
按用户本地时区显示"完成时间"。统一走 `isoformat(timespec="seconds")`。
"""

from datetime import datetime


def iso(dt: datetime | None) -> str | None:
    """ISO-8601（带时区偏移、秒精度）；非 datetime 原样转字符串兜底。"""
    if dt is None:
        return None
    try:
        return dt.isoformat(timespec="seconds")
    except (AttributeError, TypeError):
        return str(dt)


def duration_s(created_at: datetime | None,
               finished_at: datetime | None) -> float | None:
    """创建到结束的耗时（秒）。一方缺时区（历史数据）时返回 None 而不是抛错。"""
    if not created_at or not finished_at:
        return None
    try:
        return round((finished_at - created_at).total_seconds(), 1)
    except TypeError:
        return None
