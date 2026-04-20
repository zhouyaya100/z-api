"""Z API - 配置管理 (YAML)"""
import os
import secrets
import yaml
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
_ENV_PREFIX = "LITEAPI_"

def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并字典，override 覆盖 base"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def _load_yaml() -> dict:
    """加载 YAML 配置，支持环境变量覆盖"""
    config = {}
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    return config

def _env_override(config: dict) -> dict:
    """环境变量覆盖：LITEAPI_DATABASE_URL 覆盖 database.url"""
    env_map = {
        "LITEAPI_SECRET_KEY": ("security", "secret_key"),
        "LITEAPI_ADMIN_TOKEN": ("security", "admin_token"),
        "LITEAPI_DATABASE_URL": ("database", "url"),
        "LITEAPI_SERVER_PORT": ("server", "port"),
        "LITEAPI_ALLOW_REGISTER": ("registration", "allow_register"),
        "LITEAPI_DEFAULT_GROUP": ("registration", "default_group"),
    }
    for env_key, path in env_map.items():
        val = os.getenv(env_key)
        if val is not None:
            # Type conversion
            if env_key == "LITEAPI_SERVER_PORT":
                val = int(val)
            elif env_key == "LITEAPI_ALLOW_REGISTER":
                val = val.lower() in ("true", "1", "yes")
            # Set nested key
            d = config
            for p in path[:-1]:
                d = d.setdefault(p, {})
            d[path[-1]] = val
    return config


class _Settings:
    """动态配置对象，从 YAML 读取，支持环境变量覆盖"""

    def __init__(self):
        self.reload()

    def reload(self):
        raw = _load_yaml()
        raw = _env_override(raw)
        self._raw = raw
        # Flatten commonly used values for convenience
        self.SECRET_KEY = self._get("security.secret_key", "lite_api_secret_key_change_me")
        self.ADMIN_TOKEN = self._get("security.admin_token", "sk-lite-admin-token")
        self.JWT_EXPIRE_HOURS = self._get("security.jwt_expire_hours", 72)
        self.CORS_ORIGINS = self._get("security.cors_origins", "")
        self.DATABASE_URL = self._get("database.url", "sqlite+aiosqlite:///./lite_api.db")
        self.DB_POOL_SIZE = self._get("database.pool_size", 20)
        self.DB_MAX_OVERFLOW = self._get("database.max_overflow", 10)
        self.DB_POOL_RECYCLE = self._get("database.pool_recycle", 3600)
        self.DB_POOL_PRE_PING = self._get("database.pool_pre_ping", True)
        self.PROXY_TIMEOUT = self._get("proxy.timeout", 120)
        self.PROXY_MAX_CONNECTIONS = self._get("proxy.max_connections", 1000)
        self.PROXY_MAX_KEEPALIVE = self._get("proxy.max_keepalive", 100)
        self.PROXY_KEEPALIVE_EXPIRY = self._get("proxy.keepalive_expiry", 30)
        self.PROXY_RETRY_COUNT = self._get("proxy.retry_count", 1)
        self.CACHE_ENABLED = self._get("cache.enabled", True)
        self.CACHE_TTL = self._get("cache.ttl", 30)
        self.CACHE_MAX_ENTRIES = self._get("cache.max_entries", 10000)
        self.RATE_LIMIT_ENABLED = self._get("rate_limit.enabled", True)
        self.RATE_LIMIT_RPM = self._get("rate_limit.rpm", 60)
        self.RATE_LIMIT_IP_RPM = self._get("rate_limit.ip_rpm", 120)
        self.ALLOW_REGISTER = self._get("registration.allow_register", True)
        self.DEFAULT_MAX_TOKENS = self._get("registration.default_max_tokens", 3)
        self.DEFAULT_TOKEN_QUOTA = self._get("registration.default_token_quota", -1)
        self.DEFAULT_GROUP = self._get("registration.default_group", "Default")
        self.MIN_PASSWORD_LENGTH = self._get("registration.min_password_length", 8)
        self.LOG_BATCH_SIZE = self._get("log.batch_size", 50)
        self.LOG_BATCH_INTERVAL = self._get("log.batch_interval", 5)
        self.LOG_RETENTION_DAYS = self._get("log.retention_days", 90)
        self.LOG_CLEANUP_INTERVAL_HOURS = self._get("log.cleanup_interval_hours", 6)
        self.LOG_CLEANUP_BATCH_SIZE = self._get("log.cleanup_batch_size", 10000)
        self.ERROR_LOG_MAX_ENTRIES = self._get("log.error_max_entries", 10000)
        self.SERVER_HOST = self._get("server.host", "0.0.0.0")
        self.SERVER_PORT = self._get("server.port", 9000)
        self.SERVER_WORKERS = self._get("server.workers", 1)
        self.TIMEZONE_OFFSET = self._get("timezone_offset", 8)
        self.VERSION = self._get("version", "3.8.2")

        # Auto-generate secure secret_key if using default
        if self.SECRET_KEY == "lite_api_secret_key_change_me_1234567890ab":
            self.SECRET_KEY = secrets.token_hex(32)
            self._update_yaml_field("security.secret_key", self.SECRET_KEY)

        # Auto-generate secure admin_token if using default
        if self.ADMIN_TOKEN == "sk-lite-admin-token":
            self.ADMIN_TOKEN = f"sk-{secrets.token_hex(24)}"
            self._update_yaml_field("security.admin_token", self.ADMIN_TOKEN)

    def _update_yaml_field(self, dotpath: str, value: str):
        """首次启动自动生成安全配置并写回 config.yaml"""
        try:
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                keys = dotpath.split(".")
                d = cfg
                for k in keys[:-1]:
                    d = d.setdefault(k, {})
                d[keys[-1]] = value
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except Exception:
            pass  # Silently fail — not critical

    def _get(self, dotpath: str, default: Any = None) -> Any:
        """点号路径获取嵌套值"""
        keys = dotpath.split(".")
        d = self._raw
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k)
            else:
                return default
            if d is None:
                return default
        return d

    def apply_runtime(self, updates: dict):
        """热更新运行时配置并联动相关组件"""
        from .core.rate_limit import rate_limiter

        # 直接赋值到 settings 属性
        field_map = {
            "jwt_expire_hours": "JWT_EXPIRE_HOURS",
            "cors_origins": "CORS_ORIGINS",
            "proxy_timeout": "PROXY_TIMEOUT",
            "proxy_max_connections": "PROXY_MAX_CONNECTIONS",
            "proxy_max_keepalive": "PROXY_MAX_KEEPALIVE",
            "proxy_keepalive_expiry": "PROXY_KEEPALIVE_EXPIRY",
            "proxy_retry_count": "PROXY_RETRY_COUNT",
            "cache_enabled": "CACHE_ENABLED",
            "cache_ttl": "CACHE_TTL",
            "cache_max_entries": "CACHE_MAX_ENTRIES",
            "rate_limit_enabled": "RATE_LIMIT_ENABLED",
            "rate_limit_rpm": "RATE_LIMIT_RPM",
            "rate_limit_ip_rpm": "RATE_LIMIT_IP_RPM",
            "log_batch_size": "LOG_BATCH_SIZE",
            "log_batch_interval": "LOG_BATCH_INTERVAL",
            "log_retention_days": "LOG_RETENTION_DAYS",
            "log_cleanup_interval_hours": "LOG_CLEANUP_INTERVAL_HOURS",
            "log_cleanup_batch_size": "LOG_CLEANUP_BATCH_SIZE",
            "error_log_max_entries": "ERROR_LOG_MAX_ENTRIES",
            "allow_register": "ALLOW_REGISTER",
            "default_max_tokens": "DEFAULT_MAX_TOKENS",
            "default_token_quota": "DEFAULT_TOKEN_QUOTA",
            "default_group": "DEFAULT_GROUP",
            "min_password_length": "MIN_PASSWORD_LENGTH",
        }
        for key, attr in field_map.items():
            if key in updates and updates[key] is not None:
                setattr(self, attr, updates[key])

        # 联动：rate_limiter
        if rate_limiter:
            if "rate_limit_rpm" in updates and updates["rate_limit_rpm"] is not None:
                rate_limiter._rpm = updates["rate_limit_rpm"]
            if "rate_limit_ip_rpm" in updates and updates["rate_limit_ip_rpm"] is not None:
                rate_limiter._ip_rpm = updates["rate_limit_ip_rpm"]

        # 联动：error_log
        if "error_log_max_entries" in updates and updates["error_log_max_entries"] is not None:
            from .core.error_log import update_max_entries
            update_max_entries(updates["error_log_max_entries"])

    @property
    def is_sqlite(self) -> bool:
        return "sqlite" in self.DATABASE_URL

    @property
    def cors_origins_list(self) -> list[str]:
        """解析 CORS origins 为列表"""
        raw = self.CORS_ORIGINS.strip()
        if not raw:
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


settings = _Settings()
