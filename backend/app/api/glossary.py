"""术语表 API：CRUD + CSV 导入导出。"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import GlossaryEntry
from app.schemas import GlossaryIn

router = APIRouter(prefix="/api/glossary", tags=["glossary"])


@router.get("")
def list_entries(s: Session = Depends(get_session)):
    return s.exec(select(GlossaryEntry).order_by(GlossaryEntry.id)).all()


@router.post("")
def create_entry(body: GlossaryIn, s: Session = Depends(get_session)):
    if not body.source.strip() or not body.target.strip():
        raise HTTPException(400, "原文与译文都不能为空")
    e = GlossaryEntry(**body.model_dump())
    s.add(e)
    s.commit()
    s.refresh(e)
    return e


@router.put("/{entry_id}")
def update_entry(entry_id: int, body: GlossaryIn,
                 s: Session = Depends(get_session)):
    e = s.get(GlossaryEntry, entry_id)
    if not e:
        raise HTTPException(404, "术语不存在")
    for k, v in body.model_dump().items():
        setattr(e, k, v)
    s.add(e)
    s.commit()
    s.refresh(e)
    return e


@router.delete("/{entry_id}")
def delete_entry(entry_id: int, s: Session = Depends(get_session)):
    e = s.get(GlossaryEntry, entry_id)
    if not e:
        raise HTTPException(404, "术语不存在")
    s.delete(e)
    s.commit()
    return {"ok": True}


@router.get("/export")
def export_csv(s: Session = Depends(get_session)):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["source", "target", "tgt_lang", "enabled"])
    for e in s.exec(select(GlossaryEntry)).all():
        writer.writerow([e.source, e.target, e.tgt_lang, int(e.enabled)])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=glossary.csv"},
    )


@router.post("/import")
async def import_csv(file: UploadFile, s: Session = Depends(get_session)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(400, "CSV 需为 UTF-8 编码") from e
    reader = csv.DictReader(io.StringIO(text))
    required = {"source", "target"}
    if not reader.fieldnames or not required.issubset(
        {f.strip().lower() for f in reader.fieldnames}
    ):
        raise HTTPException(400, "CSV 需包含 source 与 target 列")
    count = 0
    for row in reader:
        row = { (k or "").strip().lower(): (v or "").strip()
                for k, v in row.items() }
        if not row.get("source") or not row.get("target"):
            continue
        s.add(GlossaryEntry(
            source=row["source"], target=row["target"],
            tgt_lang=row.get("tgt_lang") or "zh",
            enabled=row.get("enabled", "1") not in ("0", "false", "False"),
        ))
        count += 1
    s.commit()
    return {"ok": True, "imported": count}
