"""Z API - 报表导出路由"""
import csv
import io
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Header, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case

from ..database import get_db
from ..models import Log, User, Channel
from .auth import require_admin_by_token, require_operator_by_token, get_current_user
from ..core.security import safe_int
from ..config import settings
from ..core.utils import to_local, parse_date_filters

router = APIRouter(prefix="/api/reports", tags=["报表导出"])


def _apply_filters(q, df=None, dt=None, user_id=None, model=None, channel_id=None,
                   min_prompt_tokens=None, max_prompt_tokens=None,
                   min_completion_tokens=None, max_completion_tokens=None):
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
    if min_prompt_tokens is not None:
        q = q.where(Log.prompt_tokens >= min_prompt_tokens)
    if max_prompt_tokens is not None:
        q = q.where(Log.prompt_tokens <= max_prompt_tokens)
    if min_completion_tokens is not None:
        q = q.where(Log.completion_tokens >= min_completion_tokens)
    if max_completion_tokens is not None:
        q = q.where(Log.completion_tokens <= max_completion_tokens)
    return q


async def _fetch_log_data(db, base_filter_fn, group_by=None):
    """获取导出数据：明细或汇总"""
    if not group_by or group_by == "detail":
        # 明细导出 - 限制最多 100000 条防止 OOM
        q = select(Log).order_by(Log.id.desc()).limit(100000)
        q = base_filter_fn(q)
        result = await db.execute(q)
        logs = result.scalars().all()

        # Batch lookup users
        user_ids = list(set(l.user_id for l in logs if l.user_id))
        user_map = {}
        if user_ids:
            u_result = await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
            user_map = dict(u_result.all())

        rows = []
        for l in logs:
            rows.append({
                "id": l.id,
                "username": user_map.get(l.user_id, "-"),
                "token_name": l.token_name,
                "channel_name": l.channel_name,
                "model": l.model,
                "is_stream": l.is_stream,
                "prompt_tokens": l.prompt_tokens,
                "completion_tokens": l.completion_tokens,
                "total_tokens": l.prompt_tokens + l.completion_tokens,
                "latency_ms": l.latency_ms,
                "success": l.success,
                "error_msg": l.error_msg or "",
                "client_ip": l.client_ip,
                "created_at": to_local(l.created_at),
            })
        return rows, "detail"

    else:
        # 汇总导出
        from ..config import settings
        if group_by == "day":
            if settings.is_sqlite:
                # SQLite: UTC + offset 转本地日期
                group_expr = func.date(Log.created_at, f"+{settings.TIMEZONE_OFFSET} hours")
            else:
                # PostgreSQL: 转本地时区再截断天
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
        elif group_by == "summary":
            # User × Model × Channel cross-tab: multi-column group
            group_expr = None  # Special handling below
            group_label = "summary"
        else:
            raise HTTPException(400, "Invalid group_by, must be detail/day/user/model/channel/summary")

        if group_by == "summary":
            items_q = select(
                Log.user_id, Log.model, Log.channel_id,
                func.count(Log.id).label("requests"),
                func.sum(case((Log.success == True, 1), else_=0)).label("success"),
                func.coalesce(func.sum(Log.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Log.completion_tokens), 0).label("completion_tokens"),
                func.coalesce(func.avg(Log.latency_ms), 0).label("avg_latency_ms"),
            )
            items_q = base_filter_fn(items_q)
            items_q = items_q.group_by(Log.user_id, Log.model, Log.channel_id).order_by(func.count(Log.id).desc())
        else:
            items_q = select(
                group_expr.label(group_label),
                func.count(Log.id).label("requests"),
                func.sum(case((Log.success == True, 1), else_=0)).label("success"),
                func.coalesce(func.sum(Log.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Log.completion_tokens), 0).label("completion_tokens"),
                func.coalesce(func.avg(Log.latency_ms), 0).label("avg_latency_ms"),
            )
            items_q = base_filter_fn(items_q)
            items_q = items_q.group_by(group_expr).order_by(group_expr)
        items_result = await db.execute(items_q)

        # Batch lookup for user and channel names
        user_map = {}
        ch_map = {}
        if group_by in ("user", "summary"):
            u_result = await db.execute(select(User.id, User.username))
            user_map = dict(u_result.all())
        if group_by in ("channel", "summary"):
            ch_result = await db.execute(select(Channel.id, Channel.name))
            ch_map = dict(ch_result.all())

        rows = []
        if group_by == "summary":
            for row in items_result.all():
                uid, model_val, cid = row[0], row[1], row[2]
                username = user_map.get(uid, str(uid) if uid else "-") if uid else "-"
                ch_name = ch_map.get(cid, str(cid) if cid else "-") if cid else "-"
                rows.append({
                    "key": f"{username} / {model_val or '-'} / {ch_name}",
                    "user": username, "model": model_val or "-", "channel": ch_name,
                    "requests": row[3] or 0, "success": row[4] or 0,
                    "fail": (row[3] or 0) - (row[4] or 0),
                    "success_rate": f"{(row[4] or 0) / (row[3] or 1) * 100:.1f}%",
                    "prompt_tokens": safe_int(row[5] or 0),
                    "completion_tokens": safe_int(row[6] or 0),
                    "total_tokens": safe_int((row[5] or 0) + (row[6] or 0)),
                    "avg_latency_ms": int(row[7] or 0),
                })
        else:
            for row in items_result.all():
                key = row[0]
                if group_by == "day" and key is not None:
                    key = str(key)[:10]
                elif group_by == "user" and key is not None:
                    key = user_map.get(key, f"user:{key}")
                elif group_by == "channel" and key is not None:
                    key = ch_map.get(key, str(key))
                rows.append({
                    "key": key,
                    "requests": row.requests or 0, "success": row.success or 0,
                    "fail": (row.requests or 0) - (row.success or 0),
                    "success_rate": f"{(row.success or 0) / (row.requests or 1) * 100:.1f}%",
                    "prompt_tokens": safe_int(row.prompt_tokens or 0),
                    "completion_tokens": safe_int(row.completion_tokens or 0),
                    "total_tokens": safe_int((row.prompt_tokens or 0) + (row.completion_tokens or 0)),
                    "avg_latency_ms": int(row.avg_latency_ms or 0),
                })
        return rows, group_by


def _export_csv(rows, data_type, group_by):
    """生成 CSV"""
    final = io.StringIO()
    final.write('\ufeff')  # BOM for Excel Chinese compatibility
    if data_type == "detail":
        writer_final = csv.DictWriter(final, fieldnames=[
            "id", "username", "token_name", "channel_name", "model", "is_stream",
            "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms",
            "success", "error_msg", "client_ip", "created_at"
        ])
        writer_final.writeheader()
        writer_final.writerows(rows)
    else:
        if data_type == "summary":
            fieldnames = ["user", "model", "channel", "requests", "success", "fail", "success_rate",
                          "prompt_tokens", "completion_tokens", "total_tokens", "avg_latency_ms"]
        else:
            fieldnames = ["key", "requests", "success", "fail", "success_rate",
                          "prompt_tokens", "completion_tokens", "total_tokens", "avg_latency_ms"]
        writer_final = csv.DictWriter(final, fieldnames=fieldnames, extrasaction='ignore')
        writer_final.writeheader()
        writer_final.writerows(rows)
    return final.getvalue().encode('utf-8')


def _export_xlsx(rows, data_type, group_by):
    """生成 Excel"""
    try:
        from openpyxl import Workbook
    except ImportError:
        raise HTTPException(500, "openpyxl not installed, run: pip install openpyxl")

    wb = Workbook()
    ws = wb.active

    if data_type == "detail":
        ws.title = "请求明细"
        headers = ["ID", "用户", "令牌", "渠道", "模型", "流式", "输入Token", "输出Token",
                   "总Token", "延迟(ms)", "成功", "错误信息", "客户端IP", "时间"]
        ws.append(headers)
        for row in rows:
            ws.append([
                row["id"], row["username"], row["token_name"], row["channel_name"],
                row["model"], "是" if row["is_stream"] else "否",
                row["prompt_tokens"], row["completion_tokens"], row["total_tokens"],
                row["latency_ms"], "是" if row["success"] else "否",
                row["error_msg"], row["client_ip"], row["created_at"]
            ])
    else:
        ws.title = "用量汇总"
        if data_type == "summary":
            headers = ["用户", "模型", "渠道", "请求数", "成功数", "失败数", "成功率",
                       "输入Token", "输出Token", "总Token", "平均延迟(ms)"]
            ws.append(headers)
            for row in rows:
                ws.append([
                    row.get("user", "-"), row.get("model", "-"), row.get("channel", "-"),
                    row["requests"], row["success"], row["fail"],
                    row["success_rate"], row["prompt_tokens"], row["completion_tokens"],
                    row["total_tokens"], row["avg_latency_ms"]
                ])
        else:
            key_labels = {"day": "日期", "user": "用户", "model": "模型", "channel": "渠道"}
            headers = [key_labels.get(data_type, "维度"), "请求数", "成功数", "失败数", "成功率",
                       "输入Token", "输出Token", "总Token", "平均延迟(ms)"]
            ws.append(headers)
            for row in rows:
                ws.append([
                    row["key"], row["requests"], row["success"], row["fail"],
                    row["success_rate"], row["prompt_tokens"], row["completion_tokens"],
                    row["total_tokens"], row["avg_latency_ms"]
                ])

    # Auto column width
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_response(data, fmt, group_by, prefix="用量报表"):
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "csv":
        content = _export_csv(data[0], data[1], group_by)
        filename = f"{prefix}_{now_str}.csv"
        media_type = "text/csv"
    else:
        content = _export_xlsx(data[0], data[1], group_by)
        filename = f"{prefix}_{now_str}.xlsx"
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    # Encode filename for Content-Disposition
    from urllib.parse import quote
    encoded_filename = quote(filename)

    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )


# ---- Admin Export ----
@router.get("/export")
async def export_report(
    date_from: str = Query(None), date_to: str = Query(None),
    user_id: int = Query(None), model: str = Query(None), channel_id: int = Query(None),
    min_prompt_tokens: int = Query(None), max_prompt_tokens: int = Query(None),
    min_completion_tokens: int = Query(None), max_completion_tokens: int = Query(None),
    group_by: str = Query("detail"), fmt: str = Query("csv"),
    admin=Depends(require_admin_by_token), db: AsyncSession = Depends(get_db)
):
    # 明细导出必须指定日期范围
    if (not group_by or group_by == "detail") and not date_from and not date_to:
        raise HTTPException(400, "Detail export requires date_from or date_to to prevent OOM")
    # 日期范围最多 93 天
    df, dt = parse_date_filters(date_from, date_to)
    if df and dt and (dt - df).days > 93:
        raise HTTPException(400, "Date range cannot exceed 93 days")

    def base_filter(q):
        return _apply_filters(q, df, dt, user_id, model, channel_id,
                              min_prompt_tokens, max_prompt_tokens,
                              min_completion_tokens, max_completion_tokens)

    data = await _fetch_log_data(db, base_filter, group_by)
    return _make_response(data, fmt, group_by)


# ---- Operator Export ----
@router.get("/export/operator")
async def export_report_operator(
    date_from: str = Query(None), date_to: str = Query(None),
    user_id: int = Query(None), model: str = Query(None), channel_id: int = Query(None),
    min_prompt_tokens: int = Query(None), max_prompt_tokens: int = Query(None),
    min_completion_tokens: int = Query(None), max_completion_tokens: int = Query(None),
    group_by: str = Query("detail"), fmt: str = Query("csv"),
    op=Depends(require_operator_by_token), db: AsyncSession = Depends(get_db)
):
    # 明细导出必须指定日期范围
    if (not group_by or group_by == "detail") and not date_from and not date_to:
        raise HTTPException(400, "Detail export requires date_from or date_to to prevent OOM")
    # 日期范围最多 93 天
    df, dt = parse_date_filters(date_from, date_to)
    if df and dt and (dt - df).days > 93:
        raise HTTPException(400, "Date range cannot exceed 93 days")

    def base_filter(q):
        return _apply_filters(q, df, dt, user_id, model, channel_id,
                              min_prompt_tokens, max_prompt_tokens,
                              min_completion_tokens, max_completion_tokens)

    data = await _fetch_log_data(db, base_filter, group_by)
    return _make_response(data, fmt, group_by)


# ---- User Export ----
@router.get("/my/export")
async def export_my_report(
    date_from: str = Query(None), date_to: str = Query(None),
    model: str = Query(None), group_by: str = Query("detail"), fmt: str = Query("csv"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    # 明细导出必须指定日期范围
    if (not group_by or group_by == "detail") and not date_from and not date_to:
        raise HTTPException(400, "Detail export requires date_from or date_to to prevent OOM")
    df, dt = parse_date_filters(date_from, date_to)
    if df and dt and (dt - df).days > 93:
        raise HTTPException(400, "Date range cannot exceed 93 days")

    def base_filter(q):
        q = q.where(Log.user_id == user.id)
        return _apply_filters(q, df, dt, model=model)

    data = await _fetch_log_data(db, base_filter, group_by)
    return _make_response(data, fmt, group_by, prefix="我的用量报表")
