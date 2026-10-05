"""任务输出文件服务：支持 Range 请求（pdf.js 必需）。"""

import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models import TaskOutput

router = APIRouter(prefix="/api/tasks", tags=["files"])

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _safe_path(path: str) -> Path:
    p = Path(path).resolve()
    if not p.is_relative_to(config.DATA_DIR.resolve()):
        raise HTTPException(403, "路径越界")
    return p


@router.get("/{task_id}/files/{kind}")
def get_file(task_id: int, kind: str, range: str | None = None,
             s: Session = Depends(get_session)):
    if kind not in ("original", "mono", "dual"):
        raise HTTPException(404, "未知的文件类型")
    row = s.exec(
        select(TaskOutput).where(
            TaskOutput.task_id == task_id, TaskOutput.kind == kind
        )
    ).first()
    if not row:
        raise HTTPException(404, "输出尚未生成")
    path = _safe_path(row.path)
    if not path.exists():
        raise HTTPException(404, "文件不存在")

    size = os.path.getsize(path)
    filename = path.name
    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{filename}"',
    }

    if range:
        m = RANGE_RE.match(range.strip())
        if not m:
            raise HTTPException(416, "Range 格式无效")
        start_s, end_s = m.groups()
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            raise HTTPException(416, "Range 越界")
        length = end - start + 1
        return FileResponse(
            path, media_type="application/pdf", status_code=206,
            headers={**common_headers,
                     "Content-Range": f"bytes {start}-{end}/{size}",
                     "Content-Length": str(length)},
        )
    return FileResponse(path, media_type="application/pdf",
                        headers={**common_headers, "Content-Length": str(size)})
