"""Z API - 系统设置路由"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from ..database import get_db
from ..models import Channel, Group
from ..config import settings
from .auth import require_admin_by_token, SUPER_ADMIN_ID

router = APIRouter(prefix="/api", tags=["设置"])


async def super_admin_auth(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    """仅超级管理员可访问"""
    admin = await require_admin_by_token(authorization, db)
    if not admin.get("is_super", False):
        raise HTTPException(403, "仅超级管理员可操作")
    return admin


# ---- Schemas ----
class SettingsUpdate(BaseModel):
    # --- 可热更新 ---
    # 安全
    jwt_expire_hours: int | None = None
    cors_origins: str | None = None
    # 代理
    proxy_timeout: int | None = None
    proxy_max_connections: int | None = None
    proxy_max_keepalive: int | None = None
    proxy_keepalive_expiry: int | None = None
    proxy_retry_count: int | None = None
    # 缓存
    cache_enabled: bool | None = None
    cache_ttl: int | None = None
    cache_max_entries: int | None = None
    # 限流
    rate_limit_enabled: bool | None = None
    rate_limit_rpm: int | None = None
    rate_limit_ip_rpm: int | None = None
    # 日志
    log_batch_size: int | None = None
    log_batch_interval: int | None = None
    log_retention_days: int | None = None
    log_cleanup_interval_hours: int | None = None
    log_cleanup_batch_size: int | None = None
    error_log_max_entries: int | None = None
    # 注册
    allow_register: bool | None = None
    default_max_tokens: int | None = None
    default_token_quota: int | None = None
    default_group: str | None = None
    min_password_length: int | None = None

    def model_post_init(self, __context):
        if self.default_token_quota is not None and self.default_token_quota < -1:
            raise ValueError("default_token_quota 只能为 -1(无限) 或正数")
        if self.jwt_expire_hours is not None and self.jwt_expire_hours < 1:
            raise ValueError("JWT 过期时间至少 1 小时")
        if self.proxy_timeout is not None and self.proxy_timeout < 5:
            raise ValueError("代理超时至少 5 秒")
        if self.rate_limit_rpm is not None and self.rate_limit_rpm < 1:
            raise ValueError("RPM 限制至少 1")
        if self.rate_limit_ip_rpm is not None and self.rate_limit_ip_rpm < 1:
            raise ValueError("IP RPM 限制至少 1")
        if self.error_log_max_entries is not None and self.error_log_max_entries < 100:
            raise ValueError("错误日志最大条数至少 100")


# ---- field_name → (yaml_section, yaml_key) 映射 ----
_YAML_MAP = {
    "jwt_expire_hours": ("security", "jwt_expire_hours"),
    "cors_origins": ("security", "cors_origins"),
    "proxy_timeout": ("proxy", "timeout"),
    "proxy_max_connections": ("proxy", "max_connections"),
    "proxy_max_keepalive": ("proxy", "max_keepalive"),
    "proxy_keepalive_expiry": ("proxy", "keepalive_expiry"),
    "proxy_retry_count": ("proxy", "retry_count"),
    "cache_enabled": ("cache", "enabled"),
    "cache_ttl": ("cache", "ttl"),
    "cache_max_entries": ("cache", "max_entries"),
    "rate_limit_enabled": ("rate_limit", "enabled"),
    "rate_limit_rpm": ("rate_limit", "rpm"),
    "rate_limit_ip_rpm": ("rate_limit", "ip_rpm"),
    "log_batch_size": ("log", "batch_size"),
    "log_batch_interval": ("log", "batch_interval"),
    "log_retention_days": ("log", "retention_days"),
    "log_cleanup_interval_hours": ("log", "cleanup_interval_hours"),
    "log_cleanup_batch_size": ("log", "cleanup_batch_size"),
    "error_log_max_entries": ("log", "error_max_entries"),
    "allow_register": ("registration", "allow_register"),
    "default_max_tokens": ("registration", "default_max_tokens"),
    "default_token_quota": ("registration", "default_token_quota"),
    "default_group": ("registration", "default_group"),
    "min_password_length": ("registration", "min_password_length"),
}


# ---- Routes ----
@router.get("/settings")
async def get_settings(auth=Depends(super_admin_auth), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Group).order_by(Group.id))
    groups = [{"id": g.id, "name": g.name, "comment": g.comment} for g in result.scalars().all()]
    ch_result = await db.execute(select(Channel).where(Channel.enabled == True))
    all_models = set()
    for ch in ch_result.scalars().all():
        if ch.models:
            for m in ch.models.split(","):
                m = m.strip()
                if m:
                    all_models.add(m)
        else:
            if ch.name:
                all_models.add(ch.name)

    return {
        # 安全
        "jwt_expire_hours": settings.JWT_EXPIRE_HOURS,
        "cors_origins": settings.CORS_ORIGINS,
        # 代理
        "proxy_timeout": settings.PROXY_TIMEOUT,
        "proxy_max_connections": settings.PROXY_MAX_CONNECTIONS,
        "proxy_max_keepalive": settings.PROXY_MAX_KEEPALIVE,
        "proxy_keepalive_expiry": settings.PROXY_KEEPALIVE_EXPIRY,
        "proxy_retry_count": settings.PROXY_RETRY_COUNT,
        # 缓存
        "cache_enabled": settings.CACHE_ENABLED,
        "cache_ttl": settings.CACHE_TTL,
        "cache_max_entries": settings.CACHE_MAX_ENTRIES,
        # 限流
        "rate_limit_enabled": settings.RATE_LIMIT_ENABLED,
        "rate_limit_rpm": settings.RATE_LIMIT_RPM,
        "rate_limit_ip_rpm": settings.RATE_LIMIT_IP_RPM,
        # 日志
        "log_batch_size": settings.LOG_BATCH_SIZE,
        "log_batch_interval": settings.LOG_BATCH_INTERVAL,
        "log_retention_days": settings.LOG_RETENTION_DAYS,
        "log_cleanup_interval_hours": settings.LOG_CLEANUP_INTERVAL_HOURS,
        "log_cleanup_batch_size": settings.LOG_CLEANUP_BATCH_SIZE,
        "error_log_max_entries": settings.ERROR_LOG_MAX_ENTRIES,
        # 注册
        "allow_register": settings.ALLOW_REGISTER,
        "default_max_tokens": settings.DEFAULT_MAX_TOKENS,
        "default_token_quota": settings.DEFAULT_TOKEN_QUOTA,
        "default_group": settings.DEFAULT_GROUP,
        "min_password_length": settings.MIN_PASSWORD_LENGTH,
        # 不可热更新（只读展示）
        "server_host": settings.SERVER_HOST,
        "server_port": settings.SERVER_PORT,
        "server_workers": settings.SERVER_WORKERS,
        "database_url": settings.DATABASE_URL,
        "db_pool_size": settings.DB_POOL_SIZE,
        "db_max_overflow": settings.DB_MAX_OVERFLOW,
        # 辅助数据
        "groups": groups,
        "all_models": sorted(all_models),
    }


@router.get("/settings/public")
async def get_public_settings(db: AsyncSession = Depends(get_db)):
    """公开设置（无需鉴权，登录页用）+ 运营角色需要的分组列表"""
    groups_result = await db.execute(select(Group).order_by(Group.id))
    groups = [{"id": g.id, "name": g.name, "comment": g.comment} for g in groups_result.scalars().all()]
    return {"allow_register": settings.ALLOW_REGISTER, "groups": groups}


@router.get("/settings/error-log")
async def get_error_log(auth=Depends(super_admin_auth), offset: int = 0, limit: int = 100):
    """读取错误日志"""
    from ..core.error_log import get_error_log_content
    return get_error_log_content(offset=offset, limit=limit)


@router.delete("/settings/error-log")
async def clear_error_log(auth=Depends(super_admin_auth)):
    """清空错误日志"""
    from ..core.error_log import clear_error_log as do_clear
    if do_clear():
        return {"success": True, "message": "错误日志已清空"}
    raise HTTPException(500, "清空错误日志失败")


@router.put("/settings")
async def update_settings(data: SettingsUpdate, admin=Depends(super_admin_auth), db: AsyncSession = Depends(get_db)):
    updates = data.model_dump(exclude_unset=True)
    if not updates:
        return {"success": True, "message": "无变更"}

    # 1. 热更新内存
    settings.apply_runtime(updates)

    # 2. 写回 config.yaml
    _save_settings_to_yaml(updates)

    return {"success": True, "message": "配置已更新并保存"}


def _save_settings_to_yaml(updates: dict):
    """将变更的设置写回 config.yaml"""
    import yaml
    from pathlib import Path
    yaml_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        for field_name, value in updates.items():
            if field_name in _YAML_MAP:
                section, key = _YAML_MAP[field_name]
                cfg.setdefault(section, {})
                cfg[section][key] = value

        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        import logging
        logging.getLogger("z-api").warning(f"Failed to save settings to YAML: {e}")
