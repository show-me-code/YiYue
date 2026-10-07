"""任务输出文件服务。

Range 支持由 Starlette 的 FileResponse 原生实现（单段/多段、If-Range、
416 Range Not Satisfiable、Accept-Ranges 都会自动处理）。这里**不要**再读
`Range` 头自己拼 206：之前的写法是把 Range 绑成了查询参数
（`range: str | None = None` 在 FastAPI 里是 query 参数，不是请求头），
那段分支永远不会执行；自己实现还会丢掉多段 Range 与 If-Range。
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models import TaskOutput

router = APIRouter(prefix="/api/tasks", tags=["files"])


def _safe_path(path: str) -> Path:
    p = Path(path).resolve()
    if not p.is_relative_to(config.DATA_DIR.resolve()):
        raise HTTPException(403, "路径越界")
    return p


@router.get("/{task_id}/files/{kind}")
def get_file(task_id: int, kind: str, s: Session = Depends(get_session)):
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

    return FileResponse(
        path, media_type="application/pdf",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{path.name}"',
            # 同一个 URL 下的 PDF 会被"重试/重跑"整体替换，浏览器与 pdf.js 一旦
            # 按启发式缓存就会一直显示旧文件（实测刷新后仍是旧译文）。
            "Cache-Control": "no-store",
        },
    )
