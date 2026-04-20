"""Zapi - Main Entry"""
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from sqlalchemy import select, delete
from datetime import datetime, timedelta, timezone

from .database import init_db, AsyncSessionLocal
from .config import settings
from .models import Group, User, Log, Channel

# ---- Routers ----
from .routers import (
    auth_router, channels_router, users_router, tokens_router,
    logs_router, groups_router, settings_router, stats_router,
    reports_router, notifications_router,
)
from .routers.proxy import proxy_request, close_http_client
from .routers.auth import get_current_user

# ---- Core ----
from .core.log_writer import log_writer
from .core.quota.deductor import quota_deductor
from .core.routing.channel_pool import channel_pool
from .core.heartbeat import channel_heartbeat
from .core.error_log import error_logger

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- Startup ----
    await init_db()

    async with AsyncSessionLocal() as db:
        # Create default group
        result = await db.execute(select(Group))
        if not result.scalars().first():
            default_grp = Group(name="Default", comment="榛樿鍒嗙粍")
            db.add(default_grp)
            await db.commit()
            print("[INIT] Default group created: Default")

        # Create default admin
        from .core.security import hash_password
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        if not result.scalar_one_or_none():
            admin = User(
                username="admin",
                password_hash=hash_password("Admin@123"),
                role="admin",
                max_tokens=999,
                token_quota=-1,
                allowed_models="",
            )
            db.add(admin)
            await db.commit()
            print("[INIT] Default admin user created: admin / Admin@123")

        # Assign admin to Default group
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        admin_user = result.scalar_one_or_none()
        if admin_user and not admin_user.group_id:
            result = await db.execute(select(Group).where(Group.name == "Default"))
            grp = result.scalar_one_or_none()
            if grp:
                admin_user.group_id = grp.id
                await db.commit()
                print("[INIT] Admin user assigned to Default group")

        # Clean old logs
        if settings.LOG_RETENTION_DAYS > 0:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=settings.LOG_RETENTION_DAYS)
            await db.execute(delete(Log).where(Log.created_at < cutoff))
            await db.commit()
            print(f"[INIT] Cleaned logs older than {settings.LOG_RETENTION_DAYS} days")

        # 构建渠道池倒排索引
        result = await db.execute(select(Channel).where(Channel.enabled == True))
        channels = list(result.scalars().all())
        channel_pool.rebuild(channels)
        print(f"[INIT] Channel pool indexed: {len(channels)} channels")

    # 鍚姩鍚庡彴浠诲姟
    await log_writer.start()
    await quota_deductor.start()
    await channel_heartbeat.start()

    # Initialize error log
    from .core.error_log import setup_error_log
    setup_error_log(settings.ERROR_LOG_MAX_ENTRIES)

    print("[START] Zapi running!")
    print(f"  Gateway:  http://{settings.SERVER_HOST}:{settings.SERVER_PORT}")
    print(f"  Admin:    http://{settings.SERVER_HOST}:{settings.SERVER_PORT}/")
    print(f"  Docs:     http://{settings.SERVER_HOST}:{settings.SERVER_PORT}/docs")
    print(f"  DB:       {'SQLite' if settings.is_sqlite else 'PostgreSQL'}")
    print(f"  Cache:    {'ON' if settings.CACHE_ENABLED else 'OFF'} (TTL {settings.CACHE_TTL}s)")
    print(f"  Endpoint: POST /v1/chat/completions")
    print(f"  [!] Admin Token: {settings.ADMIN_TOKEN[:10]}...{settings.ADMIN_TOKEN[-4:]} (check config.yaml for full token)")

    yield

    # ---- Shutdown ----
    await channel_heartbeat.stop()
    await quota_deductor.stop()
    await log_writer.stop()
    await close_http_client()
    print("[STOP] Zapi shutdown complete")


app = FastAPI(title="Zapi", version=settings.VERSION, description="Lightweight OpenAI API Gateway", lifespan=lifespan)


# ---- Request body size limit (10MB) ----
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    MAX_BODY = 10 * 1024 * 1024  # 10MB
    async def dispatch(self, request: StarletteRequest, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > self.MAX_BODY:
                return JSONResponse(status_code=413, content={"error": {"message": "Request body too large (max 10MB)", "type": "invalid_request_error", "code": "payload_too_large"}})
        return await call_next(request)

app.add_middleware(BodySizeLimitMiddleware)


# ---- OpenAI-compatible error format ----
@app.exception_handler(HTTPException)
async def openai_error_handler(request: Request, exc: HTTPException):
    import json
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content={"error": detail})
    return JSONResponse(status_code=exc.status_code, content={"error": {"message": str(detail), "type": "api_error"}})


app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ---- Mount Routers ----
app.include_router(auth_router)
app.include_router(channels_router)
app.include_router(users_router)
app.include_router(tokens_router)
app.include_router(logs_router)
app.include_router(groups_router)
app.include_router(settings_router)
app.include_router(stats_router)
app.include_router(reports_router)
app.include_router(notifications_router)


# ---- OpenAI Compatible Proxy Routes ----
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await proxy_request(request)


@app.post("/v1/completions")
async def completions(request: Request):
    return await proxy_request(request)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    return await proxy_request(request)


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    return await proxy_request(request)


@app.post("/v1/audio/speech")
async def audio_speech(request: Request):
    return await proxy_request(request)


@app.get("/v1/models")
async def list_models(authorization: str = Header(default="")):
    async with AsyncSessionLocal() as db:
        from .models import Token, User
        api_key = authorization.replace("Bearer ", "").strip()
        result = await db.execute(select(Token).where(Token.key == api_key, Token.enabled == True))
        token = result.scalar_one_or_none()
        if not token:
            raise HTTPException(401, detail={"error": {"message": "Invalid API Key", "type": "invalid_request_error", "code": "invalid_api_key"}})

        # 从 channel_pool 获取模型列表 (O(1))，不再查 User/Group
        group_name = None
        user_allowed = None
        if token.user_id:
            user = await db.get(User, token.user_id)
            if user and user.group_id:
                grp = await db.get(Group, user.group_id)
                if grp:
                    group_name = grp.name
            if user and user.allowed_models:
                user_allowed = set(m.strip() for m in user.allowed_models.split(",") if m.strip())

        models_set = set(channel_pool.get_models_for_group(group_name))

        if user_allowed is not None:
            models_set = models_set & user_allowed
        if token.models:
            allowed = set(m.strip() for m in token.models.split(",") if m.strip())
            models_set = models_set & allowed
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "z-api"} for m in sorted(models_set)]}


# ---- Frontend ----
from fastapi.staticfiles import StaticFiles

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

@app.get("/")
async def serve_ui():
    static_file = BASE_DIR / "static" / "index.html"
    if static_file.exists():
        return FileResponse(str(static_file))
    return {"name": "Zapi", "version": settings.VERSION, "docs": "/docs"}


@app.get("/api/version")
async def get_version():
    return {"version": settings.VERSION}


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        workers=settings.SERVER_WORKERS,
    )

