"""Z API - 请求速率限制 (滑动窗口)

设计：当前内存实现；预留 Redis 后端接口
"""
import time
import logging
from typing import Optional, Protocol
from fastapi import HTTPException, Request
from ..config import settings

logger = logging.getLogger("z-api")


# ---- 抽象接口（预留 Redis） ----
class RateLimitBackend(Protocol):
    """限流后端协议"""
    def check(self, token_key: Optional[str], client_ip: str) -> Optional[str]: ...


class _RateLimitBucket:
    """滑动窗口速率计数器"""
    __slots__ = ("timestamps",)

    def __init__(self):
        self.timestamps: list[float] = []

    def check(self, now: float, window: float, limit: int) -> bool:
        cutoff = now - window
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        if len(self.timestamps) >= limit:
            return False
        self.timestamps.append(now)
        return True


class RateLimiter:
    """基于 IP 和 Token 的速率限制器"""

    def __init__(self, rpm: int = 60, ip_rpm: int = 120):
        self._rpm = rpm
        self._ip_rpm = ip_rpm
        self._window = 60.0
        self._token_buckets: dict[str, _RateLimitBucket] = {}
        self._ip_buckets: dict[str, _RateLimitBucket] = {}
        self._last_cleanup = time.time()

    def check(self, token_key: Optional[str], client_ip: str) -> Optional[str]:
        now = time.time()

        if now - self._last_cleanup > 300:
            self._cleanup(now)
            self._last_cleanup = now

        if client_ip and self._ip_rpm > 0:
            ip_bucket = self._ip_buckets.get(client_ip)
            if ip_bucket is None:
                ip_bucket = _RateLimitBucket()
                self._ip_buckets[client_ip] = ip_bucket
            if not ip_bucket.check(now, self._window, self._ip_rpm):
                return f"IP rate limit exceeded ({self._ip_rpm} RPM)"

        if token_key and self._rpm > 0:
            tk_bucket = self._token_buckets.get(token_key)
            if tk_bucket is None:
                tk_bucket = _RateLimitBucket()
                self._token_buckets[token_key] = tk_bucket
            if not tk_bucket.check(now, self._window, self._rpm):
                return f"Token rate limit exceeded ({self._rpm} RPM)"

        return None

    def _cleanup(self, now: float):
        cutoff = now - self._window * 2
        empty_ips = []
        for ip, bucket in self._ip_buckets.items():
            bucket.timestamps = [t for t in bucket.timestamps if t > cutoff]
            if not bucket.timestamps:
                empty_ips.append(ip)
        for ip in empty_ips:
            del self._ip_buckets[ip]

        empty_tokens = []
        for tk, bucket in self._token_buckets.items():
            bucket.timestamps = [t for t in bucket.timestamps if t > cutoff]
            if not bucket.timestamps:
                empty_tokens.append(tk)
        for tk in empty_tokens:
            del self._token_buckets[tk]

    @property
    def stats(self) -> dict:
        return {
            "active_ip_buckets": len(self._ip_buckets),
            "active_token_buckets": len(self._token_buckets),
            "rpm_limit": self._rpm,
            "ip_rpm_limit": self._ip_rpm,
        }


# ---- Redis 限流占位（未来实现） ----
# class RedisRateLimiter:
#     """Redis 限流后端（未来实现）
#     使用 Redis SORTED SET 实现滑动窗口
#     多 worker 共享限流状态
#     """
#     def __init__(self, redis_url: str, rpm: int = 60, ip_rpm: int = 120):
#         ...
#     async def check(self, token_key, client_ip) -> Optional[str]:
#         ...


# 全局实例
rate_limiter = RateLimiter(
    rpm=settings.RATE_LIMIT_RPM,
    ip_rpm=settings.RATE_LIMIT_IP_RPM,
) if settings.RATE_LIMIT_ENABLED else None
