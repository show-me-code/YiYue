"""配置档 API：CRUD + 连接测试。"""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import Profile
from app.schemas import ProfileIn, ProfileOut

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _mask(key: str) -> str:
    if not key:
        return ""
    return "•••" + key[-4:] if len(key) > 4 else "•••"


def _out(p: Profile) -> ProfileOut:
    return ProfileOut(
        id=p.id, name=p.name, base_url=p.base_url, model=p.model, qps=p.qps,
        pool_max_workers=p.pool_max_workers,
        custom_system_prompt=p.custom_system_prompt, is_default=p.is_default,
        api_key_masked=_mask(p.api_key),
    )


def _ensure_single_default(s: Session, profile: Profile) -> None:
    if profile.is_default:
        for other in s.exec(select(Profile).where(Profile.id != profile.id)):
            other.is_default = False
            s.add(other)
        s.commit()


@router.get("", response_model=list[ProfileOut])
def list_profiles(s: Session = Depends(get_session)):
    return [_out(p) for p in s.exec(select(Profile)).all()]


@router.post("", response_model=ProfileOut)
def create_profile(body: ProfileIn, s: Session = Depends(get_session)):
    p = Profile(**body.model_dump())
    if not p.is_default and not s.exec(select(Profile)).first():
        p.is_default = True  # 第一个配置档自动设为默认
    s.add(p)
    s.commit()
    s.refresh(p)
    _ensure_single_default(s, p)
    s.refresh(p)
    return _out(p)


@router.put("/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: int, body: ProfileIn,
                   s: Session = Depends(get_session)):
    p = s.get(Profile, profile_id)
    if not p:
        raise HTTPException(404, "配置档不存在")
    data = body.model_dump()
    # 未提供新 key 时保留旧 key
    if not data.get("api_key"):
        data.pop("api_key")
    for k, v in data.items():
        setattr(p, k, v)
    s.add(p)
    s.commit()
    s.refresh(p)
    _ensure_single_default(s, p)
    s.refresh(p)
    return _out(p)


@router.delete("/{profile_id}")
def delete_profile(profile_id: int, s: Session = Depends(get_session)):
    p = s.get(Profile, profile_id)
    if not p:
        raise HTTPException(404, "配置档不存在")
    s.delete(p)
    s.commit()
    return {"ok": True}


@router.post("/{profile_id}/test")
def test_profile(profile_id: int, s: Session = Depends(get_session)):
    p = s.get(Profile, profile_id)
    if not p:
        raise HTTPException(404, "配置档不存在")
    url = (p.base_url.rstrip("/") if p.base_url
           else "https://api.openai.com/v1") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {p.api_key}"} if p.api_key else {},
            json={
                "model": p.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return {"ok": True, "detail": "连接成功"}
        if "json" in resp.headers.get("content-type", ""):
            detail = resp.json().get("error", {}).get("message", resp.text[:200])
        else:
            detail = resp.text[:200]
        return {"ok": False, "detail": f"HTTP {resp.status_code}: {detail}"}
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"连接失败：{e}"}
