"""Z API - 认证路由"""
import re
from fastapi import APIRouter, HTTPException, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from ..database import get_db
from ..models import User, Token, Group
from ..config import settings
from ..core.security import (
    hash_password, verify_password, validate_password_strength,
    create_jwt, decode_jwt, safe_int, generate_captcha, verify_captcha,
    check_login_rate, record_login_failure, record_login_success,
)
from sqlalchemy import func

router = APIRouter(prefix="/api/auth", tags=["认证"])


# ---- Dependencies ----
async def get_current_user(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)) -> User:
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(401, "Not authenticated")
    payload = decode_jwt(token)
    user_id = int(payload.get("sub", 0))
    if not user_id:
        raise HTTPException(401, "Invalid token")
    user = await db.get(User, user_id)
    if not user or not user.enabled:
        raise HTTPException(401, "User not found or disabled")
    return user


async def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Admin required")
    return user


SUPER_ADMIN_ID = 1  # 超级管理员固定为 ID=1

async def admin_auth(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    """管理员鉴权依赖（公共，供各 router import）"""
    return await require_admin_by_token(authorization, db)

async def operator_auth(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    """运营角色鉴权依赖（公共，供各 router import）"""
    return await require_operator_by_token(authorization, db)

async def require_admin_by_token(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    token = authorization.replace("Bearer ", "").strip()
    # 1. Admin token (静态 token) — 视为超级管理员
    if token == settings.ADMIN_TOKEN:
        return {"role": "admin", "user_id": None, "is_super": True}
    # 2. JWT token — 从 DB 校验角色
    try:
        payload = decode_jwt(token)
        user_id = int(payload.get("sub", 0))
        if not user_id:
            raise HTTPException(401, "Invalid token")
        user = await db.get(User, user_id)
        if not user or not user.enabled:
            raise HTTPException(401, "User not found or disabled")
        if user.role != "admin":
            raise HTTPException(403, "Admin access required")
        return {"role": "admin", "user_id": user.id, "is_super": user.id == SUPER_ADMIN_ID}
    except HTTPException:
        raise
    except:
        pass
    raise HTTPException(401, "Invalid authentication")


async def require_operator_by_token(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    """运营角色鉴权：admin token 或 DB 中角色为 admin/operator 的用户可通过，校验用户 enabled 状态"""
    token = authorization.replace("Bearer ", "").strip()
    # 1. Admin token (静态 token)
    if token == settings.ADMIN_TOKEN:
        return {"role": "admin", "user_id": None}
    # 2. JWT token — 从 DB 校验角色（不信 JWT 里的 role，因为 admin 可能已改了角色）
    try:
        payload = decode_jwt(token)
        user_id = int(payload.get("sub", 0))
        if not user_id:
            raise HTTPException(401, "Invalid token")
        user = await db.get(User, user_id)
        if not user or not user.enabled:
            raise HTTPException(401, "User not found or disabled")
        if user.role not in ("admin", "operator"):
            raise HTTPException(403, "Insufficient role, admin or operator required")
        return {"role": user.role, "user_id": user.id}
    except HTTPException:
        raise
    except:
        pass
    raise HTTPException(401, "Invalid authentication")


# ---- Schemas ----
class RegisterRequest(BaseModel):
    username: str
    password: str
    captcha_id: str = ""
    captcha_code: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: str = ""
    captcha_code: str = ""


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ---- Routes ----
@router.post("/register")
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if not data.captcha_id or not data.captcha_code:
        raise HTTPException(400, "请输入验证码")
    if not verify_captcha(data.captcha_id, data.captcha_code):
        raise HTTPException(400, "验证码错误或已过期")
    if not settings.ALLOW_REGISTER:
        raise HTTPException(403, "注册已关闭，请联系管理员")
    username = data.username.strip()
    if len(username) < 2 or len(username) > 32:
        raise HTTPException(400, "用户名需 2-32 个字符")
    if not re.match(r'^[a-zA-Z0-9_\u4e00-\u9fff]+$', username):
        raise HTTPException(400, "用户名只能包含字母、数字、下划线和中文")
    pwd_error = validate_password_strength(data.password)
    if pwd_error:
        raise HTTPException(400, pwd_error)
    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        raise HTTPException(400, "用户名已存在")
    group_id = None
    if settings.DEFAULT_GROUP:
        grp = (await db.execute(select(Group).where(Group.name == settings.DEFAULT_GROUP))).scalar_one_or_none()
        if grp:
            group_id = grp.id
    user = User(
        username=username,
        password_hash=hash_password(data.password),
        role="user",
        group_id=group_id,
        max_tokens=settings.DEFAULT_MAX_TOKENS,
        token_quota=settings.DEFAULT_TOKEN_QUOTA,
        allowed_models="",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    jwt_token = create_jwt(user.id, user.role)
    return {
        "success": True,
        "user": {"id": user.id, "username": user.username, "role": user.role},
        "token": jwt_token,
    }


@router.get("/captcha")
async def get_captcha():
    from fastapi.responses import Response
    captcha_id, img_bytes = generate_captcha()
    return Response(content=img_bytes, media_type="image/png", headers={"X-Captcha-Id": captcha_id})


@router.post("/login")
async def login(data: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # 登录暴力破解防护：username + IP 双维度
    client_ip = request.client.host if request.client else "unknown"
    check_login_rate(data.username.strip().lower())
    check_login_rate(f"ip:{client_ip}")

    if not data.captcha_id or not data.captcha_code:
        raise HTTPException(400, "请输入验证码")
    if not verify_captcha(data.captcha_id, data.captcha_code):
        raise HTTPException(400, "验证码错误或已过期")
    username = data.username.strip()
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password_hash):
        record_login_failure(username.strip().lower())
        record_login_failure(f"ip:{client_ip}")
        raise HTTPException(401, "用户名或密码错误")
    if not user.enabled:
        raise HTTPException(403, "账号已被禁用")
    record_login_success(username.strip().lower())
    record_login_success(f"ip:{client_ip}")
    jwt_token = create_jwt(user.id, user.role)
    return {
        "success": True,
        "user": {"id": user.id, "username": user.username, "role": user.role,
                 "max_tokens": user.max_tokens, "allowed_models": user.allowed_models,
                 "token_quota": safe_int(user.token_quota), "token_quota_used": safe_int(user.token_quota_used),
                 "is_super": user.id == SUPER_ADMIN_ID},
        "token": jwt_token,
    }


@router.get("/me")
async def get_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    token_count = (await db.execute(
        select(func.count(Token.id)).where(Token.user_id == user.id)
    )).scalar()
    return {
        "id": user.id, "username": user.username, "role": user.role,
        "enabled": user.enabled, "max_tokens": user.max_tokens,
        "allowed_models": user.allowed_models,
        "token_quota": safe_int(user.token_quota), "token_quota_used": safe_int(user.token_quota_used),
        "token_count": token_count,
        "can_create_token": token_count < user.max_tokens,
        "group_id": user.group_id,
        "is_super": user.id == SUPER_ADMIN_ID,
    }


@router.put("/password")
async def change_password(data: ChangePasswordRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    pwd_error = validate_password_strength(data.new_password)
    if pwd_error:
        raise HTTPException(400, pwd_error)
    if data.new_password == data.old_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    user.password_hash = hash_password(data.new_password)
    await db.commit()
    return {"success": True}
