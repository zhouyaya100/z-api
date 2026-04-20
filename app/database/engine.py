"""Z API - 数据库引擎 (PostgreSQL/SQLite 自适应)"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

from ..config import settings

_engine_kwargs = {
    "echo": False,
    "pool_pre_ping": settings.DB_POOL_PRE_PING,
    "pool_recycle": settings.DB_POOL_RECYCLE,
}

if settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"timeout": 15}
    _engine_kwargs["pool_size"] = 1
    _engine_kwargs["max_overflow"] = 0
else:
    _engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
    _engine_kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """初始化数据库：建表 + SQLite WAL 模式"""
    from .base import Base
    async with engine.begin() as conn:
        if settings.is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=5000"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI Depends 用的生成器"""
    async with AsyncSessionLocal() as session:
        yield session
