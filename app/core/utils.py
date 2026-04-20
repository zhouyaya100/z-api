"""Z API - 公共工具函数

消除 stats.py / logs.py 重复的时区转换和日期过滤逻辑
"""
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from ..config import settings

# 本地时区
_TZ_OFFSET = timezone(timedelta(hours=settings.TIMEZONE_OFFSET))
_TZ_HOURS = settings.TIMEZONE_OFFSET


def to_local(utc_dt) -> str | None:
    """UTC datetime → 本地时间字符串"""
    if not utc_dt:
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(_TZ_OFFSET).strftime("%Y-%m-%d %H:%M:%S")


def parse_date_filters(date_from=None, date_to=None):
    """解析日期筛选参数（本地日期 → UTC），返回 (df, dt) datetime 对象"""
    df, dt = None, None
    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d") - timedelta(hours=_TZ_HOURS)
        except ValueError:
            raise HTTPException(400, "Invalid date_from format, use YYYY-MM-DD")
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1) - timedelta(hours=_TZ_HOURS)
        except ValueError:
            raise HTTPException(400, "Invalid date_to format, use YYYY-MM-DD")
    return df, dt
