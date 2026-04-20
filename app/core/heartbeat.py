"""Zapi - 渠道心跳检测"""
import asyncio
import json
import time
import threading
import socket
import subprocess
import re
import logging
import httpx
from datetime import datetime, timezone

from ..config import settings
from ..models.channel import Channel
from ..models.notification import Notification
from ..models.user import User

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

logger = logging.getLogger("z-api")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[HEARTBEAT] %(message)s'))
    logger.addHandler(handler)


def _get_local_ips():
    """获取本机所有 IP（启动时缓存）"""
    ips = {'127.0.0.1', 'localhost', '0.0.0.0'}
    try:
        for iface in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(iface[4][0])
    except:
        pass
    try:
        result = subprocess.run(['ipconfig'], capture_output=True, timeout=5)
        text = result.stdout.decode('gbk', errors='ignore')
        for ip in re.findall(r'(\d+\.\d+\.\d+\.\d+)', text):
            ips.add(ip)
    except:
        pass
    return ips


# 启动时计算一次，之后不再重复
_LOCAL_IPS = None


class ChannelHeartbeat:
    """定期检测渠道可用性，故障/恢复时写入通知中心"""

    def __init__(self):
        self._thread = None
        self._running = False
        self._fail_streak: dict[int, int] = {}
        self._notified: set[int] = set()
        # 记录故障开始时间，用于恢复通知显示故障持续时长
        self._fault_start: dict[int, datetime] = {}

    async def start(self):
        self._running = True
        global _LOCAL_IPS
        _LOCAL_IPS = _get_local_ips()
        logger.info(f"Local IPs: {_LOCAL_IPS}")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Thread started")

    async def stop(self):
        self._running = False
        logger.info("Stopping...")

    def _run_loop(self):
        """线程主循环：首次延迟10秒，之后每5分钟检测"""
        import time as _time
        logger.info("Run loop started, waiting 10s for startup...")
        _time.sleep(10)  # 等服务启动
        if not self._running:
            logger.info("Stopped before first check")
            return
        # 首次检测
        self._do_check("first")
        # 定期检测
        check_count = 0
        logger.info("Entering periodic check loop...")
        while self._running:
            try:
                # 分段 sleep，方便退出
                for i in range(30):  # 30 * 10s = 300s = 5min
                    if not self._running:
                        return
                    _time.sleep(10)
                check_count += 1
                logger.info(f"Starting check #{check_count}...")
                self._do_check(f"#{check_count}")
                logger.info(f"After check #{check_count}, running={self._running}")
            except Exception as e:
                import traceback
                logger.error(f"Run loop iteration error: {e}\n{traceback.format_exc()}")

    def _do_check(self, label: str):
        """执行一次完整检测（独立事件循环 + 独立数据库引擎）"""
        loop = None
        engine = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            engine = create_async_engine(settings.DATABASE_URL, pool_size=2, max_overflow=0)
            sf = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
            loop.run_until_complete(self._check_all(sf))
            logger.info(f"Check {label} done")
        except Exception as e:
            import traceback
            logger.error(f"Check {label} error: {e}\n{traceback.format_exc()}")
        finally:
            # 先清理引擎，再关闭事件循环
            if engine and loop:
                try:
                    loop.run_until_complete(engine.dispose())
                except:
                    pass
            if loop:
                try:
                    # 排空所有待处理的回调，防止 loop.close() 时有残留的 asyncpg 任务
                    loop.run_until_complete(asyncio.sleep(0.1))
                except:
                    pass
                try:
                    loop.close()
                except:
                    pass

    async def _check_all(self, sf):
        """一次检测所有渠道（使用外部传入的 session factory，引擎由 _do_check 管理）"""
        try:
            # 1. 读取渠道
            async with sf() as db:
                result = await db.execute(select(Channel))
                channels = list(result.scalars().all())

            # 2. 读取管理员 ID（一次查询）
            async with sf() as db:
                r = await db.execute(select(User.id).where(User.role == "admin", User.enabled == True))
                admin_ids = [row[0] for row in r.all()]

            # 3. 逐渠道检测
            for ch in channels:
                if not self._running:
                    break
                try:
                    ok, latency = await self._test_one(ch)
                except Exception:
                    ok, latency = False, 0

                if not ok:
                    self._fail_streak[ch.id] = self._fail_streak.get(ch.id, 0) + 1
                    should_alert = (ch.enabled and self._fail_streak[ch.id] >= 2) or (not ch.enabled and ch.id not in self._notified)
                    if should_alert and ch.id not in self._notified:
                        await self._write_notifications(sf, admin_ids,
                            category="fault",
                            title=f"渠道故障: {ch.name}",
                            content=f"渠道 [{ch.name}] (ID:{ch.id}) 心跳检测失败，当前状态: {'已禁用' if not ch.enabled else '运行中'}。上游: {ch.base_url}")
                        self._notified.add(ch.id)
                        self._fault_start[ch.id] = datetime.now(timezone.utc).replace(tzinfo=None)
                else:
                    if ch.id in self._notified:
                        down_since = self._fault_start.pop(ch.id, None)
                        duration = ""
                        if down_since:
                            delta = datetime.now(timezone.utc).replace(tzinfo=None) - down_since
                            mins = int(delta.total_seconds() / 60)
                            if mins >= 60:
                                duration = f"，故障持续约 {mins // 60}小时{mins % 60}分钟"
                            else:
                                duration = f"，故障持续约 {mins}分钟"
                        await self._write_notifications(sf, admin_ids,
                            category="recovery",
                            title=f"渠道恢复: {ch.name}",
                            content=f"渠道 [{ch.name}] (ID:{ch.id}) 已恢复正常运行，延迟 {latency}ms{duration}。上游: {ch.base_url}")
                        self._notified.discard(ch.id)
                    self._fail_streak.pop(ch.id, None)
                    self._fault_start.pop(ch.id, None)

                # 4. 更新数据库
                async with sf() as db:
                    db_ch = await db.get(Channel, ch.id)
                    if db_ch:
                        db_ch.test_time = datetime.now(timezone.utc).replace(tzinfo=None)
                        if ok:
                            db_ch.fail_count = 0
                            db_ch.response_time = latency
                            if not db_ch.enabled:
                                db_ch.enabled = True
                        else:
                            db_ch.fail_count = (db_ch.fail_count or 0) + 1
                            db_ch.response_time = 0
                            if db_ch.auto_ban and db_ch.fail_count >= 3 and db_ch.enabled:
                                db_ch.enabled = False
                        await db.commit()
                # 同步 channel_pool 索引
                from .routing.channel_pool import channel_pool
                channel_pool.update_fail_count(ch.id, db_ch.fail_count or 0, db_ch.enabled)
        except Exception as e:
            logger.error(f"check_all error: {e}")
            raise

    async def _test_one(self, ch: Channel) -> tuple[bool, int]:
        """测试单个渠道，返回 (是否成功, 延迟ms)"""
        # 回环检测
        if _LOCAL_IPS:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(ch.base_url)
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                if port == settings.SERVER_PORT:
                    target_ips = set()
                    for addr in socket.getaddrinfo(host, None):
                        target_ips.add(addr[4][0])
                    if target_ips & _LOCAL_IPS:
                        return True, 0  # 跳过回环
            except:
                pass

        test_model = ch.models.split(",")[0].strip() if ch.models else None
        if not test_model:
            return True, 0

        model_to_use = test_model
        if ch.model_mapping:
            try:
                mapping = json.loads(ch.model_mapping)
                model_to_use = mapping.get(test_model, test_model)
            except:
                pass

        base = ch.base_url.rstrip("/")
        test_url = (base + "/chat/completions") if base.endswith("/v1") else (base + "/v1/chat/completions")
        headers = {"Authorization": f"Bearer {ch.api_key}", "Content-Type": "application/json"}
        body = json.dumps({"model": model_to_use, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1})

        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(test_url, content=body, headers=headers)
                latency = int((time.time() - start) * 1000)
                return resp.status_code == 200, latency
        except:
            return False, 0

    async def _write_notifications(self, sf, admin_ids, category, title, content):
        """写入通知（复用 session factory）"""
        async with sf() as db:
            for admin_id in admin_ids:
                db.add(Notification(
                    category=category, title=title, content=content,
                    sender_id=None, receiver_id=admin_id,
                ))
            await db.commit()


channel_heartbeat = ChannelHeartbeat()
