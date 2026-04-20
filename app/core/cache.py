"""Z API - 内存缓存 (Token/Channel 热点数据)

设计：当前进程内 TTL 缓存；预留 Redis L2 接口
"""
import time
import logging
from typing import Any, Optional, Protocol
from ..config import settings

logger = logging.getLogger("z-api")


# ---- 抽象接口（预留 Redis） ----
class CacheBackend(Protocol):
    """缓存后端协议，当前实现为 MemoryCache，未来可替换为 RedisCache"""
    def get(self, key: str) -> Optional[Any]: ...
    def set(self, key: str, value: Any, ttl: int | None = None): ...
    def delete(self, key: str): ...
    def clear(self): ...


class _CacheEntry:
    __slots__ = ("value", "expires")

    def __init__(self, value: Any, ttl: int):
        self.value = value
        self.expires = time.time() + ttl


class MemoryCache:
    """带 TTL + 最大条目数的内存缓存"""

    def __init__(self, ttl: int = 30, max_entries: int = 10000):
        self._store: dict[str, _CacheEntry] = {}
        self._ttl = ttl
        self._max_entries = max_entries

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.time() > entry.expires:
            del self._store[key]
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl: int | None = None):
        if len(self._store) >= self._max_entries:
            self.cleanup()
        if len(self._store) >= self._max_entries:
            self._evict_oldest(len(self._store) // 4 + 1)
        self._store[key] = _CacheEntry(value, ttl or self._ttl)

    def delete(self, key: str):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()

    def cleanup(self):
        now = time.time()
        expired = [k for k, v in self._store.items() if now > v.expires]
        for k in expired:
            del self._store[k]
        if expired:
            logger.debug(f"Cache cleanup: removed {len(expired)} expired entries, {len(self._store)} remaining")

    def _evict_oldest(self, count: int):
        sorted_keys = sorted(self._store.keys(), key=lambda k: self._store[k].expires)
        for k in sorted_keys[:count]:
            del self._store[k]

    @property
    def size(self) -> int:
        return len(self._store)


# ---- Redis 缓存占位（未来实现） ----
# class RedisCache:
#     """Redis 缓存后端（未来实现）
#     接口与 MemoryCache 一致，替换即可
#     """
#     def __init__(self, redis_url: str, ttl: int = 30, prefix: str = "zapi:cache:"):
#         self._redis = ...  # aioredis
#         self._ttl = ttl
#         self._prefix = prefix
#
#     async def get(self, key: str) -> Optional[Any]:
#         ...
#     async def set(self, key: str, value: Any, ttl: int | None = None):
#         ...
#     async def delete(self, key: str):
#         ...
#     async def clear(self):
#         ...


# 全局缓存实例
_cache_ttl = settings.CACHE_TTL if settings.CACHE_ENABLED else 0
_max_entries = settings.CACHE_MAX_ENTRIES if settings.CACHE_ENABLED else 0
token_cache = MemoryCache(ttl=_cache_ttl, max_entries=_max_entries) if _cache_ttl else None
channel_cache = MemoryCache(ttl=_cache_ttl, max_entries=_max_entries) if _cache_ttl else None
