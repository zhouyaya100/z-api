"""Z API - 令牌管理路由"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
import secrets

from ..database import get_db
from ..models import User, Token
from .auth import require_admin_by_token, get_current_user, admin_auth
from ..core.security import safe_int

router = APIRouter(prefix="/api", tags=["令牌管理"])



# ---- Schemas ----
class TokenCreate(BaseModel):
    name: str
    models: str = ""
    quota_limit: int = -1
    user_id: int | None = None  # Admin can specify target user

    def model_post_init(self, __context):
        if self.quota_limit < -1:
            raise ValueError("quota_limit 只能为 -1(无限) 或正数")


class TokenUpdate(BaseModel):
    name: str | None = None
    models: str | None = None
    enabled: bool | None = None
    quota_limit: int | None = None

    def model_post_init(self, __context):
        if self.quota_limit is not None and self.quota_limit < -1:
            raise ValueError("quota_limit 只能为 -1(无限) 或正数")


class RechargeRequest(BaseModel):
    amount: int

    def model_post_init(self, __context):
        if self.amount <= 0:
            raise ValueError("充值数量必须大于0")


# ---- Admin Routes ----
@router.get("/tokens")
async def list_tokens(admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Token).order_by(Token.id))
    tokens = result.scalars().all()
    # 批量查用户名 — 消除 N+1
    user_ids = list(set(t.user_id for t in tokens if t.user_id))
    user_map = {}
    if user_ids:
        u_result = await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
        user_map = dict(u_result.all())
    out = []
    for t in tokens:
        out.append({
            "id": t.id, "user_id": t.user_id, "username": user_map.get(t.user_id, "-"),
            "name": t.name, "key": "***" + t.key[-4:] if len(t.key) > 4 else "***", "models": t.models,
            "enabled": t.enabled, "quota_limit": safe_int(t.quota_limit), "quota_used": safe_int(t.quota_used),
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None
        })
    return out


# ---- User Routes ----
@router.get("/my/tokens")
async def list_my_tokens(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Token).where(Token.user_id == user.id).order_by(Token.id))
    tokens = result.scalars().all()
    return [{
        "id": t.id, "name": t.name, "key": t.key, "models": t.models,
        "enabled": t.enabled, "quota_limit": safe_int(t.quota_limit), "quota_used": safe_int(t.quota_used),
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None
    } for t in tokens]


@router.post("/tokens")
async def create_token(data: TokenCreate, authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    """创建令牌：普通用户创建自己的，管理员可指定 user_id"""
    from .auth import require_admin_by_token
    # Try admin first
    admin_info = None
    try:
        admin_info = await require_admin_by_token(authorization, db)
    except:
        pass
    if admin_info and admin_info.get("role") == "admin":
        # Admin creating token — use user_id from data or default to super admin
        target_uid = getattr(data, 'user_id', None) or admin_info.get("user_id") or 1
        user = await db.get(User, target_uid)
        if not user:
            raise HTTPException(404, f"User {target_uid} not found")
    else:
        # Regular user creating own token
        user = await get_current_user(authorization, db)
    token_count = (await db.execute(select(func.count(Token.id)).where(Token.user_id == user.id))).scalar()
    if token_count >= user.max_tokens:
        raise HTTPException(403, f"令牌数量已达上限 ({user.max_tokens})，请联系管理员")
    if data.models and user.allowed_models:
        requested = set(m.strip() for m in data.models.split(",") if m.strip())
        allowed = set(m.strip() for m in user.allowed_models.split(",") if m.strip())
        if not requested.issubset(allowed):
            forbidden = requested - allowed
            raise HTTPException(403, f"无权使用模型: {', '.join(forbidden)}")
    key = f"sk-{secrets.token_hex(24)}"
    token = Token(user_id=user.id, name=data.name, key=key, models=data.models, quota_limit=data.quota_limit)
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return {"success": True, "id": token.id, "key": key}


async def _resolve_admin_or_user(authorization: str, db: AsyncSession):
    """解析身份：admin token/返回 (user, is_admin)，否则普通用户"""
    from .auth import require_admin_by_token
    admin_info = None
    try:
        admin_info = await require_admin_by_token(authorization, db)
    except:
        pass
    if admin_info and admin_info.get("role") == "admin":
        uid = admin_info.get("user_id") or 1
        user = await db.get(User, uid) if uid else None
        return user, True
    user = await get_current_user(authorization, db)
    return user, False


@router.put("/tokens/{token_id}")
async def update_token(token_id: int, data: TokenUpdate, authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    user, is_admin = await _resolve_admin_or_user(authorization, db)
    tk = await db.get(Token, token_id)
    if not tk:
        raise HTTPException(404, "Token not found")
    if is_admin:
        if data.models is not None: tk.models = data.models
        if data.quota_limit is not None: tk.quota_limit = data.quota_limit
        if data.name is not None: tk.name = data.name
        if data.enabled is not None: tk.enabled = data.enabled
    else:
        if tk.user_id != user.id:
            raise HTTPException(403, "Not your token")
        if data.enabled is not None: tk.enabled = data.enabled
        if data.models is not None:
            new_models = [m.strip() for m in data.models.split(",") if m.strip()]
            if new_models and user.allowed_models:
                allowed = set(m.strip() for m in user.allowed_models.split(",") if m.strip())
                if not set(new_models).issubset(allowed):
                    raise HTTPException(403, "无权使用部分模型")
            tk.models = data.models
    await db.commit()
    return {"success": True}


@router.post("/tokens/{token_id}/recharge")
async def recharge_token(token_id: int, data: RechargeRequest, authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    user, is_admin = await _resolve_admin_or_user(authorization, db)
    tk = await db.get(Token, token_id)
    if not tk:
        raise HTTPException(404, "Token not found")
    if not is_admin and tk.user_id != user.id:
        raise HTTPException(403, "Not your token")
    if tk.quota_limit == -1:
        raise HTTPException(400, "令牌额度为无限，无需充值")
    tk.quota_limit += data.amount
    await db.commit()
    await db.refresh(tk)
    return {"success": True, "quota_limit": safe_int(tk.quota_limit), "quota_used": safe_int(tk.quota_used)}


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: int, authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    user, is_admin = await _resolve_admin_or_user(authorization, db)
    tk = await db.get(Token, token_id)
    if not tk:
        raise HTTPException(404, "Token not found")
    if not is_admin and tk.user_id != user.id:
        raise HTTPException(403, "Not your token")
    await db.delete(tk)
    await db.commit()
    return {"success": True}
