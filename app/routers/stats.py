"""Z API - 统计与仪表盘路由"""
from fastapi import APIRouter, Depends, Header, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, case
from datetime import datetime, timedelta, timezone

from ..database import get_db
from ..models import Channel, Token, Log, User, Group
from .auth import require_admin_by_token, require_operator_by_token, get_current_user, admin_auth, operator_auth
from ..core.security import safe_int
from ..config import settings
from ..core.auth_models import get_group_authed_models
from ..core.utils import to_local, parse_date_filters

router = APIRouter(prefix="/api", tags=["统计"])




def _apply_usage_filters(q, df=None, dt=None, user_id=None, model=None, channel_id=None):
    """应用用量查询的通用筛选"""
    if df:
        q = q.where(Log.created_at >= df)
    if dt:
        q = q.where(Log.created_at < dt)
    if user_id is not None:
        q = q.where(Log.user_id == user_id)
    if model:
        safe_model = model.replace("%", r"\%").replace("_", r"\_")
        q = q.where(Log.model.ilike(f"%{safe_model}%", escape="\\"))
    if channel_id is not None:
        q = q.where(Log.channel_id == channel_id)
    return q





async def _build_usage_response(db, base_filter_fn, group_by="day", page=1, page_size=50, order="desc"):
    """构建用量汇总响应"""
    # Summary
    summary_q = select(
        func.count(Log.id).label("total_requests"),
        func.sum(case((Log.success == True, 1), else_=0)).label("success_requests"),
        func.coalesce(func.sum(Log.prompt_tokens), 0).label("total_prompt_tokens"),
        func.coalesce(func.sum(Log.completion_tokens), 0).label("total_completion_tokens"),
        func.coalesce(func.avg(Log.latency_ms), 0).label("avg_latency_ms"),
    )
    summary_q = base_filter_fn(summary_q)
    summary_row = (await db.execute(summary_q)).one()

    summary = {
        "total_requests": summary_row.total_requests or 0,
        "success_requests": summary_row.success_requests or 0,
        "total_prompt_tokens": safe_int(summary_row.total_prompt_tokens or 0),
        "total_completion_tokens": safe_int(summary_row.total_completion_tokens or 0),
        "total_tokens": safe_int((summary_row.total_prompt_tokens or 0) + (summary_row.total_completion_tokens or 0)),
        "avg_latency_ms": int(summary_row.avg_latency_ms or 0),
    }

    # Full detail table: user × model × channel cross-tab
    if group_by == "detail":
        detail_q = select(
            Log.user_id, Log.model, Log.channel_id,
            func.count(Log.id).label("requests"),
            func.sum(case((Log.success == True, 1), else_=0)).label("success"),
            func.coalesce(func.sum(Log.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(Log.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.avg(Log.latency_ms), 0).label("avg_latency_ms"),
        )
        detail_q = base_filter_fn(detail_q)
        detail_q = detail_q.group_by(Log.user_id, Log.model, Log.channel_id).order_by(desc("requests"))
        # Count total
        count_q = select(func.count()).select_from(detail_q.subquery())
        total = (await db.execute(count_q)).scalar() or 0
        # Paginate
        detail_q = detail_q.offset((page - 1) * page_size).limit(page_size)
        detail_result = await db.execute(detail_q)
        # Batch resolve usernames and channel names
        user_ids = set()
        channel_ids = set()
        for row in detail_result.all():
            if row.user_id: user_ids.add(row.user_id)
            if row.channel_id: channel_ids.add(row.channel_id)
        user_map = dict((await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))).all()) if user_ids else {}
        ch_map = dict((await db.execute(select(Channel.id, Channel.name).where(Channel.id.in_(channel_ids)))).all()) if channel_ids else {}
        # Re-execute since we consumed the result
        detail_result = await db.execute(detail_q)
        items = []
        for row in detail_result.all():
            items.append({
                "key": f"{user_map.get(row.user_id, str(row.user_id))} / {row.model or '-'} / {ch_map.get(row.channel_id, str(row.channel_id))}",
                "user": user_map.get(row.user_id, str(row.user_id) if row.user_id else "-"),
                "model": row.model or "-",
                "channel": ch_map.get(row.channel_id, str(row.channel_id) if row.channel_id else "-"),
                "requests": row.requests or 0,
                "success": row.success or 0,
                "prompt_tokens": safe_int(row.prompt_tokens or 0),
                "completion_tokens": safe_int(row.completion_tokens or 0),
                "total_tokens": safe_int((row.prompt_tokens or 0) + (row.completion_tokens or 0)),
                "avg_latency_ms": int(row.avg_latency_ms or 0),
            })
        return {"summary": summary, "items": items, "total": total, "page": page, "page_size": page_size}

    # Grouped items
    if group_by == "day":
        if settings.is_sqlite:
            group_expr = func.date(Log.created_at, f"+{settings.TIMEZONE_OFFSET} hours")
        else:
            group_expr = func.date_trunc("day", Log.created_at + timedelta(hours=settings.TIMEZONE_OFFSET))
        group_label = "period"
    elif group_by == "user":
        group_expr = Log.user_id
        group_label = "user_id"
    elif group_by == "model":
        group_expr = Log.model
        group_label = "model"
    elif group_by == "channel":
        group_expr = Log.channel_id
        group_label = "channel_id"
    else:
        raise HTTPException(400, "Invalid group_by, must be day/user/model/channel/detail")

    items_q = select(
        group_expr.label(group_label),
        func.count(Log.id).label("requests"),
        func.sum(case((Log.success == True, 1), else_=0)).label("success"),
        func.coalesce(func.sum(Log.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(Log.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.avg(Log.latency_ms), 0).label("avg_latency_ms"),
    )
    items_q = base_filter_fn(items_q)
    # 排序：day 默认按日期降序（表格最新在上），asc 时按日期升序（趋势图需要）
    # 其他 group_by 按请求数降序
    if group_by == "day":
        if order == "asc":
            items_q = items_q.group_by(group_expr).order_by(group_expr)
        else:
            items_q = items_q.group_by(group_expr).order_by(desc(group_expr))
    else:
        items_q = items_q.group_by(group_expr).order_by(desc("requests"))
    # Count total
    count_q = select(func.count()).select_from(items_q.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    # Paginate
    items_q = items_q.offset((page - 1) * page_size).limit(page_size)
    items_result = await db.execute(items_q)
    # Batch resolve keys for user/channel group_by
    rows = items_result.all()
    if group_by == "user":
        uids = set(r[0] for r in rows if r[0] is not None)
        umap = dict((await db.execute(select(User.id, User.username).where(User.id.in_(uids)))).all()) if uids else {}
    elif group_by == "channel":
        cids = set(r[0] for r in rows if r[0] is not None)
        cmap = dict((await db.execute(select(Channel.id, Channel.name).where(Channel.id.in_(cids)))).all()) if cids else {}
    items = []
    for row in rows:
        key = row[0]
        if group_by == "day" and key is not None:
            key = str(key)[:10]
        elif group_by == "user" and key is not None:
            key = umap.get(key, f"user:{key}")
        elif group_by == "channel" and key is not None:
            key = cmap.get(key, str(key))
        items.append({
            "key": key,
            "requests": row.requests or 0,
            "success": row.success or 0,
            "prompt_tokens": safe_int(row.prompt_tokens or 0),
            "completion_tokens": safe_int(row.completion_tokens or 0),
            "total_tokens": safe_int((row.prompt_tokens or 0) + (row.completion_tokens or 0)),
            "avg_latency_ms": int(row.avg_latency_ms or 0),
        })

    return {"summary": summary, "items": items, "total": total, "page": page, "page_size": page_size}


# ---- Admin/Operator Stats ----
@router.get("/stats")
async def get_stats(auth=Depends(operator_auth), db: AsyncSession = Depends(get_db)):
    # 合并查询：用一条 SQL 拿全量统计，避免 12 次独立查询
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)

    # 合并渠道/用户/令牌计数 (3条SQL → 1条子查询)
    channels_q = await db.execute(select(
        func.count(Channel.id).label("total"),
        func.sum(case((Channel.enabled == True, 1), else_=0)).label("enabled"),
    ))
    ch_row = channels_q.one()

    users_q = await db.execute(select(
        func.count(User.id).label("total"),
        func.sum(case((User.enabled == True, 1), else_=0)).label("enabled"),
    ))
    u_row = users_q.one()

    tokens_q = await db.execute(select(
        func.count(Token.id).label("total"),
        func.sum(case((Token.enabled == True, 1), else_=0)).label("enabled"),
    ))
    tk_row = tokens_q.one()

    # 合并日志统计 (9条SQL → 1条)
    log_q = await db.execute(select(
        func.count(Log.id).label("total_requests"),
        func.sum(case((Log.success == True, 1), else_=0)).label("success_requests"),
        func.coalesce(func.sum(Log.prompt_tokens), 0).label("total_prompt"),
        func.coalesce(func.sum(Log.completion_tokens), 0).label("total_completion"),
        func.coalesce(func.avg(Log.latency_ms), 0).label("avg_latency"),
        func.sum(case((Log.created_at >= since, 1), else_=0)).label("recent_requests"),
        func.coalesce(func.sum(case((Log.created_at >= since, Log.prompt_tokens + Log.completion_tokens), else_=0)), 0).label("recent_tokens"),
    ))
    log_row = log_q.one()

    return {
        "channels": ch_row.total or 0, "channels_enabled": int(ch_row.enabled or 0),
        "users": u_row.total or 0, "users_enabled": int(u_row.enabled or 0),
        "tokens": tk_row.total or 0, "tokens_enabled": int(tk_row.enabled or 0),
        "total_requests": log_row.total_requests or 0,
        "success_requests": log_row.success_requests or 0,
        "total_tokens": safe_int((log_row.total_prompt or 0) + (log_row.total_completion or 0)),
        "total_prompt_tokens": safe_int(log_row.total_prompt or 0),
        "total_completion_tokens": safe_int(log_row.total_completion or 0),
        "avg_latency_ms": int(log_row.avg_latency or 0),
        "recent_24h_requests": log_row.recent_requests or 0,
        "recent_24h_tokens": safe_int(log_row.recent_tokens or 0),
    }


@router.get("/dashboard")
async def dashboard(auth=Depends(operator_auth), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Log).order_by(Log.id.desc()).limit(10))
    recent = [{"model": l.model, "latency_ms": l.latency_ms, "success": l.success,
               "created_at": to_local(l.created_at)} for l in result.scalars().all()]
    result = await db.execute(
        select(Log.model, func.count(Log.id).label("count"), func.avg(Log.latency_ms).label("avg_latency"))
        .group_by(Log.model).order_by(desc("count")).limit(10))
    model_stats = [{"model": r[0], "count": r[1], "avg_latency": int(r[2] or 0)} for r in result.all()]
    return {"recent_logs": recent, "model_stats": model_stats}


# ---- Admin Usage Summary ----
async def _usage_summary_impl(db, date_from, date_to, user_id, model, channel_id, group_by, page, page_size, order="desc"):
    """用量查询公共逻辑"""
    df, dt = parse_date_filters(date_from, date_to)

    def base_filter(q):
        return _apply_usage_filters(q, df, dt, user_id, model, channel_id)

    return await _build_usage_response(db, base_filter, group_by, page, page_size, order)


@router.get("/stats/usage")
async def get_usage_summary(date_from: str = Query(None), date_to: str = Query(None),
                             user_id: int = Query(None), model: str = Query(None),
                             channel_id: int = Query(None),
                             group_by: str = Query("day"),
                             page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                             order: str = Query("desc"),
                             admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    return await _usage_summary_impl(db, date_from, date_to, user_id, model, channel_id, group_by, page, page_size, order)


@router.get("/stats/usage/operator")
async def get_usage_summary_operator(date_from: str = Query(None), date_to: str = Query(None),
                                      user_id: int = Query(None), model: str = Query(None),
                                      channel_id: int = Query(None),
                                      group_by: str = Query("day"),
                                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                                      order: str = Query("desc"),
                                      op=Depends(operator_auth), db: AsyncSession = Depends(get_db)):
    return await _usage_summary_impl(db, date_from, date_to, user_id, model, channel_id, group_by, page, page_size, order)
@router.get("/my/models")
async def my_available_models(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    models = await get_group_authed_models(db, user.group_id, user.allowed_models)
    grp = await db.get(Group, user.group_id) if user.group_id else None
    return {"models": models, "group": grp.name if grp else ""}


# ---- User Dashboard ----
@router.get("/my/dashboard")
async def user_dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # 合并查询：减少数据库访问次数
    token_count = (await db.execute(select(func.count(Token.id)).where(Token.user_id == user.id))).scalar()

    # 一次查询拿全部统计数据
    stats_q = await db.execute(select(
        func.count(Log.id).label("total_requests"),
        func.sum(case((Log.success == True, 1), else_=0)).label("success_requests"),
        func.coalesce(func.sum(Log.prompt_tokens), 0).label("total_prompt"),
        func.coalesce(func.sum(Log.completion_tokens), 0).label("total_completion"),
    ).where(Log.user_id == user.id))
    stats_row = stats_q.one()

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    recent_q = await db.execute(select(
        func.count(Log.id).label("reqs"),
        func.coalesce(func.sum(Log.prompt_tokens + Log.completion_tokens), 0).label("tokens"),
    ).where(Log.user_id == user.id, Log.created_at >= since))
    recent_row = recent_q.one()

    result = await db.execute(
        select(Log.model, func.count(Log.id).label("count"), func.avg(Log.latency_ms).label("avg_latency"))
        .where(Log.user_id == user.id).group_by(Log.model).order_by(desc("count")).limit(10))
    model_stats = [{"model": r[0], "count": r[1], "avg_latency": int(r[2] or 0)} for r in result.all()]

    result = await db.execute(select(Log).where(Log.user_id == user.id).order_by(Log.id.desc()).limit(20))
    recent_logs = [{
        "model": l.model, "prompt_tokens": l.prompt_tokens, "completion_tokens": l.completion_tokens,
        "latency_ms": l.latency_ms, "success": l.success,
        "created_at": to_local(l.created_at)
    } for l in result.scalars().all()]

    grp = await db.get(Group, user.group_id) if user.group_id else None
    authed_models = await get_group_authed_models(db, user.group_id, user.allowed_models)

    total_prompt = safe_int(stats_row.total_prompt or 0)
    total_completion = safe_int(stats_row.total_completion or 0)

    return {
        "token_count": token_count, "max_tokens": user.max_tokens,
        "token_quota": safe_int(user.token_quota), "token_quota_used": safe_int(user.token_quota_used),
        "group_name": grp.name if grp else "",
        "authorized_models": authed_models,
        "total_requests": stats_row.total_requests or 0, "success_requests": stats_row.success_requests or 0,
        "total_prompt_tokens": total_prompt, "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "recent_24h_requests": recent_row.reqs or 0, "recent_24h_tokens": safe_int(recent_row.tokens or 0),
        "model_stats": model_stats, "recent_logs": recent_logs
    }


# ---- User Usage Summary ----
@router.get("/my/usage")
async def my_usage_summary(date_from: str = Query(None), date_to: str = Query(None),
                           model: str = Query(None), group_by: str = Query("day"),
                           page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                           order: str = Query("desc"),
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    df, dt = parse_date_filters(date_from, date_to)

    def base_filter(q):
        q = q.where(Log.user_id == user.id)
        return _apply_usage_filters(q, df, dt, model=model)

    return await _build_usage_response(db, base_filter, group_by, page, page_size, order)
