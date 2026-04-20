"""Z API - 配额扣减器

职责：原子扣减配额，支持批量合并
设计：当前用 DB 原子更新 + 内存合并队列；预留 Redis 后端接口
"""
import asyncio
import time
import logging
from collections import defaultdict
from sqlalchemy import update, text

from ...database import AsyncSessionLocal
from ...models import Token, User
from ...core.error_log import error_logger

logger = logging.getLogger("z-api")


class QuotaDeductor:
    """配额扣减器

    当前实现：DB 原子 SQL + 内存批量合并队列
    未来可替换为：Redis INCRBY + 异步落库
    """

    def __init__(self, flush_interval: float = 5.0):
        self._flush_interval = flush_interval
        # 合并队列: token_id → 累计扣减量
        self._token_queue: dict[int, int] = defaultdict(int)
        # 合并队列: user_id → 累计扣减量
        self._user_queue: dict[int, int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """启动后台批量落库任务"""
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self):
        """停止并刷出剩余"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()

    async def deduct(self, token_id: int, user_id: int | None, tokens_used: int):
        """记录扣减（内存合并，定时落库）"""
        if tokens_used <= 0:
            return
        async with self._lock:
            self._token_queue[token_id] += tokens_used
            if user_id:
                self._user_queue[user_id] += tokens_used

    async def deduct_immediate(self, token_id: int, user_id: int | None, tokens_used: int):
        """立即扣减（不走队列，用于需要即时反馈的场景）"""
        if tokens_used <= 0:
            return
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Token).where(Token.id == token_id)
                    .values(quota_used=Token.quota_used + tokens_used)
                )
                if user_id:
                    await db.execute(
                        update(User).where(User.id == user_id)
                        .values(token_quota_used=User.token_quota_used + tokens_used)
                    )
                await db.commit()
        except Exception as e:
            logger.error(f"Immediate quota deduct failed: {e}")

    @staticmethod
    def _build_case_when_sql(table_name: str, column: str, batch: dict[int, int]) -> str:
        """构建 CASE WHEN 批量更新 SQL

        UPDATE table SET col = CASE id WHEN 1 THEN col + 10 WHEN 2 THEN col + 20 END WHERE id IN (1, 2)
        """
        if not batch:
            return ""
        when_clauses = " ".join(f"WHEN {id} THEN {column} + {amount}" for id, amount in batch.items())
        id_list = ",".join(str(id) for id in batch.keys())
        return f"UPDATE {table_name} SET {column} = CASE id {when_clauses} END WHERE id IN ({id_list})"

    async def flush(self):
        """将内存中的扣减合并写入 DB（CASE WHEN 批量更新，单条 SQL）"""
        async with self._lock:
            if not self._token_queue and not self._user_queue:
                return
            token_batch = dict(self._token_queue)
            user_batch = dict(self._user_queue)
            self._token_queue.clear()
            self._user_queue.clear()

        try:
            async with AsyncSessionLocal() as db:
                if token_batch:
                    sql = self._build_case_when_sql("tokens", "quota_used", token_batch)
                    await db.execute(text(sql))
                if user_batch:
                    sql = self._build_case_when_sql("users", "token_quota_used", user_batch)
                    await db.execute(text(sql))
                await db.commit()
            logger.debug(f"Quota flush: {len(token_batch)} tokens, {len(user_batch)} users")
        except Exception as e:
            error_logger.error(f"Quota flush failed: {e}", exc_info=True)

    async def _flush_loop(self):
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except Exception as e:
                logger.warning(f"Quota flush loop error: {e}")

    @property
    def pending(self) -> int:
        return len(self._token_queue) + len(self._user_queue)


# 全局实例
quota_deductor = QuotaDeductor()
