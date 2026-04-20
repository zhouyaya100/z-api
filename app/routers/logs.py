"""Z API - 日志路由"""
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime, timedelta, timezone

from ..database import get_db
from ..models import Log, User
from .auth import require_admin_by_token, require_operator_by_token, admin_auth, operator_auth
from ..core.security import safe_int
from ..config import settings
from ..core.utils import to_local, parse_date_filters



router = APIRouter(prefix="/api", tags=["日志"])




def _apply_log_filters(q, total_q, model=None, success=None, user_id=None, username=None,
                       min_prompt_tokens=None, max_prompt_tokens=None,
                       min_completion_tokens=None, max_completion_tokens=None,
                       date_from=None, date_to=None, channel_id=None):
    """公共筛选逻辑，同时应用到数据查询和总数查询"""
    if model:
        safe_model = model.replace("%", r"\%").replace("_", r"\_")
        q = q.where(Log.model.ilike(f"%{safe_model}%", escape="\\"))
        total_q = total_q.where(Log.model.ilike(f"%{safe_model}%", escape="\\"))
    if success is not None and success != "":
        sv = success == "1"
        q = q.where(Log.success == sv)
        total_q = total_q.where(Log.success == sv)
    if user_id is not None:
        q = q.where(Log.user_id == user_id)
        total_q = total_q.where(Log.user_id == user_id)
    if min_prompt_tokens is not None:
        q = q.where(Log.prompt_tokens >= min_prompt_tokens)
        total_q = total_q.where(Log.prompt_tokens >= min_prompt_tokens)
    if max_prompt_tokens is not None:
        q = q.where(Log.prompt_tokens <= max_prompt_tokens)
        total_q = total_q.where(Log.prompt_tokens <= max_prompt_tokens)
    if min_completion_tokens is not None:
        q = q.where(Log.completion_tokens >= min_completion_tokens)
        total_q = total_q.where(Log.completion_tokens >= min_completion_tokens)
    if max_completion_tokens is not None:
        q = q.where(Log.completion_tokens <= max_completion_tokens)
        total_q = total_q.where(Log.completion_tokens <= max_completion_tokens)
    if date_from or date_to:
        df, dt = parse_date_filters(date_from, date_to)
        if df:
            q = q.where(Log.created_at >= df)
            total_q = total_q.where(Log.created_at >= df)
        if dt:
            q = q.where(Log.created_at < dt)
            total_q = total_q.where(Log.created_at < dt)
    if channel_id is not None:
        q = q.where(Log.channel_id == channel_id)
        total_q = total_q.where(Log.channel_id == channel_id)
    if username:
        # username 需要子查询
        from sqlalchemy import exists
        subq = select(User.id).where(User.username.ilike(f"%{username}%"))
        q = q.where(Log.user_id.in_(subq))
        total_q = total_q.where(Log.user_id.in_(subq))
    return q, total_q


def _build_log_response(logs, total, user_map):
    return {"total": total, "items": [{
        "id": l.id, "user_id": l.user_id, "username": user_map.get(l.user_id, "-"), "token_name": l.token_name,
        "channel_name": l.channel_name,
        "model": l.model, "is_stream": l.is_stream,
        "prompt_tokens": l.prompt_tokens, "completion_tokens": l.completion_tokens,
        "latency_ms": l.latency_ms, "success": l.success,
        "error_msg": l.error_msg[:500] if l.error_msg else "",
        "client_ip": l.client_ip,
        "created_at": to_local(l.created_at)
    } for l in logs]}


async def _list_logs_impl(db, limit, offset, model, success, user_id, username,
                          min_prompt_tokens, max_prompt_tokens,
                          min_completion_tokens, max_completion_tokens,
                          date_from, date_to, channel_id):
    """日志查询公共逻辑"""
    q = select(Log).order_by(Log.id.desc())
    total_q = select(func.count(Log.id))
    q, total_q = _apply_log_filters(q, total_q, model=model, success=success,
                                     user_id=user_id, username=username,
                                     min_prompt_tokens=min_prompt_tokens, max_prompt_tokens=max_prompt_tokens,
                                     min_completion_tokens=min_completion_tokens, max_completion_tokens=max_completion_tokens,
                                     date_from=date_from, date_to=date_to, channel_id=channel_id)
    total = (await db.execute(total_q)).scalar() or 0
    result = await db.execute(q.offset(offset).limit(limit))
    logs = result.scalars().all()
    user_ids = list(set(l.user_id for l in logs if l.user_id))
    user_map = {}
    if user_ids:
        u_result = await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
        user_map = dict(u_result.all())
    return _build_log_response(logs, total, user_map)


@router.get("/logs")
async def list_logs(limit: int = Query(50, le=500), offset: int = Query(0, ge=0),
                    model: str = Query(None), success: str = Query(None),
                    user_id: int = Query(None), username: str = Query(None),
                    min_prompt_tokens: int = Query(None), max_prompt_tokens: int = Query(None),
                    min_completion_tokens: int = Query(None), max_completion_tokens: int = Query(None),
                    date_from: str = Query(None), date_to: str = Query(None),
                    channel_id: int = Query(None),
                    admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    return await _list_logs_impl(db, limit, offset, model, success, user_id, username,
                                min_prompt_tokens, max_prompt_tokens,
                                min_completion_tokens, max_completion_tokens,
                                date_from, date_to, channel_id)


@router.get("/logs/operator")
async def list_logs_operator(limit: int = Query(50, le=500), offset: int = Query(0, ge=0),
                              model: str = Query(None), success: str = Query(None),
                              user_id: int = Query(None), username: str = Query(None),
                              min_prompt_tokens: int = Query(None), max_prompt_tokens: int = Query(None),
                              min_completion_tokens: int = Query(None), max_completion_tokens: int = Query(None),
                              date_from: str = Query(None), date_to: str = Query(None),
                              channel_id: int = Query(None),
                              op=Depends(operator_auth), db: AsyncSession = Depends(get_db)):
    return await _list_logs_impl(db, limit, offset, model, success, user_id, username,
                                min_prompt_tokens, max_prompt_tokens,
                                min_completion_tokens, max_completion_tokens,
                                date_from, date_to, channel_id)
