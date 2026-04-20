"""Z API - 渠道管理路由"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
import json
import httpx
import time
from datetime import datetime, timezone

from ..database import get_db
from ..models import Channel
from .auth import require_admin_by_token, require_operator_by_token, admin_auth, operator_auth
from ..core.routing.channel_pool import channel_pool

router = APIRouter(prefix="/api", tags=["渠道管理"])




# ---- Schemas ----
class ChannelCreate(BaseModel):
    name: str
    type: str = "openai"
    base_url: str
    api_key: str
    models: str = ""
    model_mapping: str = ""
    allowed_groups: str = ""
    weight: int = 1
    priority: int = 0
    auto_ban: bool = True


class ChannelUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: str | None = None
    model_mapping: str | None = None
    allowed_groups: str | None = None
    weight: int | None = None
    priority: int | None = None
    enabled: bool | None = None
    auto_ban: bool | None = None


# ---- Routes ----
@router.get("/channels")
async def list_channels(auth=Depends(operator_auth), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Channel).order_by(Channel.id))
    channels = result.scalars().all()
    return [{
        "id": c.id, "name": c.name, "type": c.type,
        "base_url": c.base_url,
        "api_key": "***" + c.api_key[-4:] if len(c.api_key) > 4 else "***",
        "api_key_length": len(c.api_key),
        "models": c.models, "model_mapping": c.model_mapping, "allowed_groups": c.allowed_groups,
        "weight": c.weight, "priority": c.priority,
        "enabled": c.enabled, "auto_ban": c.auto_ban,
        "fail_count": c.fail_count,
        "test_time": c.test_time.isoformat() if c.test_time else None,
        "response_time": c.response_time,
        "created_at": c.created_at.isoformat() if c.created_at else None
    } for c in channels]


def _normalize_model_mapping(mapping_str: str) -> str:
    """将模型映射转为标准 JSON，支持多种输入格式"""
    if not mapping_str:
        return ""
    mapping_str = mapping_str.strip()
    # 已经是合法 JSON
    try:
        obj = json.loads(mapping_str)
        if isinstance(obj, dict):
            return json.dumps(obj, ensure_ascii=False)
    except:
        pass
    # key:value 格式，逗号或换行分隔  例: "gpt-4:gpt-4-0613, claude-3:claude-3-sonnet"
    result = {}
    for pair in mapping_str.replace("\n", ",").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            k, v = pair.split(":", 1)
            result[k.strip()] = v.strip()
        elif "=" in pair:
            k, v = pair.split("=", 1)
            result[k.strip()] = v.strip()
        else:
            raise HTTPException(400, f"模型映射格式错误: '{pair}'，请使用 key:value 格式或 JSON")
    if not result:
        return ""
    return json.dumps(result, ensure_ascii=False)


@router.post("/channels")
async def create_channel(data: ChannelCreate, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    mapping_str = _normalize_model_mapping(data.model_mapping or "")
    ch = Channel(name=data.name, type=data.type, base_url=data.base_url,
                 api_key=data.api_key, models=data.models, model_mapping=mapping_str,
                 allowed_groups=data.allowed_groups,
                 weight=data.weight, priority=data.priority, auto_ban=data.auto_ban)
    db.add(ch)
    await db.commit()
    await db.refresh(ch)
    # 增量更新渠道池索引
    channel_pool.update_channel(ch)
    return {"success": True, "id": ch.id}


@router.put("/channels/{channel_id}")
async def update_channel(channel_id: int, data: ChannelUpdate, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    ch = await db.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    update_data = data.model_dump(exclude_unset=True)
    if 'model_mapping' in update_data:
        update_data['model_mapping'] = _normalize_model_mapping(update_data['model_mapping'] or "")
    # 防止脱敏的 api_key 覆盖真实值
    if 'api_key' in update_data and update_data['api_key'] and update_data['api_key'].startswith('***'):
        del update_data['api_key']
    for k, v in update_data.items():
        setattr(ch, k, v)
    await db.commit()
    # 增量更新渠道池索引
    await db.refresh(ch)
    channel_pool.update_channel(ch)
    return {"success": True}


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    ch = await db.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    await db.delete(ch)
    await db.commit()
    # 从渠道池移除
    channel_pool.remove_channel(channel_id)
    return {"success": True}


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: int, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    ch = await db.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    test_model = None
    if ch.models:
        test_model = ch.models.split(",")[0].strip()
    if not test_model:
        return {"success": False, "latency_ms": 0, "model": "-", "status": "No model configured"}
    model_to_use = test_model
    if ch.model_mapping:
        try:
            mapping = json.loads(ch.model_mapping)
            model_to_use = mapping.get(test_model, test_model)
        except:
            pass
    base = ch.base_url.rstrip("/")
    test_url = (base + "/chat/completions") if base.endswith("/v1") else (base + "/v1/chat/completions")
    headers = {"Authorization": f"Bearer {ch.api_key}", "Content-Type": "application/json"}
    body = json.dumps({"model": model_to_use, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5})
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(test_url, content=body, headers=headers)
            latency_ms = int((time.time() - start) * 1000)
            ch.test_time = datetime.now(timezone.utc).replace(tzinfo=None)
            ch.response_time = latency_ms
            if resp.status_code == 200:
                ch.fail_count = 0
                if not ch.enabled:
                    ch.enabled = True
                await db.commit()
                channel_pool.update_channel(ch)
                return {"success": True, "latency_ms": latency_ms, "model": model_to_use, "status": "OK"}
            else:
                ch.fail_count = (ch.fail_count or 0) + 1
                if ch.auto_ban and ch.fail_count >= 5:
                    ch.enabled = False
                await db.commit()
                channel_pool.update_channel(ch)
                return {"success": False, "latency_ms": latency_ms, "model": model_to_use,
                        "status": f"HTTP {resp.status_code}", "error": resp.text[:300]}
    except (httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout):
        ch.test_time = datetime.now(timezone.utc).replace(tzinfo=None)
        ch.response_time = 0
        ch.fail_count = (ch.fail_count or 0) + 1
        if ch.auto_ban and ch.fail_count >= 5:
            ch.enabled = False
        await db.commit()
        channel_pool.update_channel(ch)
        return {"success": False, "latency_ms": 0, "model": model_to_use, "status": "Timeout"}
    except Exception as e:
        ch.test_time = datetime.now(timezone.utc).replace(tzinfo=None)
        ch.response_time = 0
        ch.fail_count = (ch.fail_count or 0) + 1
        if ch.auto_ban and ch.fail_count >= 5:
            ch.enabled = False
        await db.commit()
        channel_pool.update_channel(ch)
        return {"success": False, "latency_ms": 0, "model": model_to_use, "status": "Error", "error": str(e)[:200]}
