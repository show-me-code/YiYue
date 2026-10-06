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
        id=p.id, name=p.name, base_url=p.base_url, model=p.model,
        protocol=getattr(p, "protocol", "openai") or "openai",
        qps=p.qps,
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


def _probe(profile: Profile, protocol: str) -> tuple[bool, str]:
    """按指定协议探测端点连通性。"""
    base = profile.base_url.rstrip("/") if profile.base_url else None
    if protocol == "anthropic":
        url = (base or "https://api.anthropic.com") + "/v1/messages"
        headers = {"x-api-key": profile.api_key,
                   "anthropic-version": "2023-06-01"}
        body: dict = {"model": profile.model, "max_tokens": 8,
                      "messages": [{"role": "user", "content": "ping"}]}
    else:
        url = (base or "https://api.openai.com/v1") + "/chat/completions"
        headers = {"Authorization": f"Bearer {profile.api_key}"} \
            if profile.api_key else {}
        body = {"model": profile.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1}
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=20)
        if resp.status_code == 200:
            return True, "连接成功"
        if "json" in resp.headers.get("content-type", ""):
            try:
                detail = resp.json().get("error", {}).get("message",
                                                         resp.text[:200])
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
        else:
            detail = f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code}: {detail}"
    except httpx.HTTPError as e:
        return False, f"连接失败：{e}"


@router.post("/{profile_id}/test")
def test_profile(profile_id: int, s: Session = Depends(get_session)):
    p = s.get(Profile, profile_id)
    if not p:
        raise HTTPException(404, "配置档不存在")
    protocol = getattr(p, "protocol", "openai") or "openai"
    ok, detail = _probe(p, protocol)
    result: dict = {"ok": ok, "detail": detail, "protocol": protocol}
    if not ok:
        other = "anthropic" if protocol == "openai" else "openai"
        other_ok, other_detail = _probe(p, other)
        if other_ok:
            result["suggest_protocol"] = other
            result["detail"] = (
                f"当前协议（{protocol}）探测失败：{detail}；"
                f"但该端点支持 {other} 协议，可一键切换。"
            )
    return result
