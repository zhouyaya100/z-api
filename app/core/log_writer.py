"""Z API - 异步批量日志写入"""
import asyncio
import time
import logging
from typing import List
from ..config import settings
from ..database import AsyncSessionLocal
from ..models import Log

logger = logging.getLogger("z-api")


class LogBatchWriter:
    """批量收集日志，定时或定量写入数据库"""

    def __init__(self, batch_size: int = 50, interval: float = 5.0, max_retries: int = 2):
        self._batch_size = batch_size
        self._interval = interval
        self._max_retries = max_retries
        self._queue: List[Log] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._running = False
        self._cache_cleanup_fn = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())
        if settings.LOG_RETENTION_DAYS > 0:
            interval_s = settings.LOG_CLEANUP_INTERVAL_HOURS * 3600
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(interval_s))
            logger.info(f"Log cleanup task started: every {settings.LOG_CLEANUP_INTERVAL_HOURS}h, retention={settings.LOG_RETENTION_DAYS}d")

    async def stop(self):
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def add(self, log: Log):
        async with self._lock:
            self._queue.append(log)
            if len(self._queue) >= self._batch_size:
                await self._do_flush()

    async def add_nowait(self, **kwargs):
        log = Log(**kwargs)
        async with self._lock:
            self._queue.append(log)
            if len(self._queue) >= self._batch_size:
                asyncio.create_task(self._do_flush())

    async def _flush_loop(self):
        while self._running:
            await asyncio.sleep(self._interval)
            try:
                await self._flush()
            except Exception as e:
                logger.warning(f"Log flush error: {e}")
            try:
                if self._cache_cleanup_fn:
                    self._cache_cleanup_fn()
            except Exception:
                pass

    async def _cleanup_loop(self, interval_s: float):
        while self._running:
            await asyncio.sleep(interval_s)
            try:
                await self._cleanup_old_logs()
            except Exception as e:
                logger.warning(f"Log cleanup error: {e}")

    async def _cleanup_old_logs(self):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import delete, select

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=settings.LOG_RETENTION_DAYS)
        batch_size = settings.LOG_CLEANUP_BATCH_SIZE

        total_deleted = 0
        while True:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Log.id).where(Log.created_at < cutoff).limit(batch_size)
                )
                ids_to_delete = [row[0] for row in result.all()]
                if not ids_to_delete:
                    break
                await db.execute(delete(Log).where(Log.id.in_(ids_to_delete)))
                await db.commit()
                total_deleted += len(ids_to_delete)
                if len(ids_to_delete) < batch_size:
                    break
            await asyncio.sleep(0.1)

        if total_deleted > 0:
            logger.info(f"Log cleanup: deleted {total_deleted} logs older than {settings.LOG_RETENTION_DAYS} days")

    async def _flush(self):
        async with self._lock:
            await self._do_flush()

    async def _do_flush(self):
        if not self._queue:
            return
        batch = self._queue[:self._batch_size]
        self._queue = self._queue[self._batch_size:]

        for attempt in range(self._max_retries + 1):
            try:
                async with AsyncSessionLocal() as db:
                    db.add_all(batch)
                    await db.commit()
                return
            except Exception as e:
                if attempt < self._max_retries:
                    logger.warning(f"Log batch write attempt {attempt+1} failed ({len(batch)} logs), retrying: {e}")
                    await asyncio.sleep(1)
                else:
                    logger.error(f"Log batch write failed after {self._max_retries+1} attempts ({len(batch)} logs LOST): {e}")

    @property
    def pending(self) -> int:
        return len(self._queue)


# 全局实例
log_writer = LogBatchWriter(
    batch_size=settings.LOG_BATCH_SIZE,
    interval=settings.LOG_BATCH_INTERVAL,
)
