"""Z API - 用户管理路由"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
import re

from ..database import get_db
from ..models import User, Token, Group
from ..config import settings
from .auth import require_admin_by_token, get_current_user, SUPER_ADMIN_ID, admin_auth
from ..core.security import hash_password, safe_int
from ..core.auth_models import get_group_authed_models

router = APIRouter(prefix="/api", tags=["用户管理"])



# ---- Schemas ----
class UserUpdate(BaseModel):
    enabled: bool | None = None
    max_tokens: int | None = None
    token_quota: int | None = None
    token_quota_used: int | None = None
    allowed_models: str | None = None
    group_id: int | None = None
    password: str | None = None
    role: str | None = None


class UserRechargeRequest(BaseModel):
    amount: int

    def model_post_init(self, __context):
        if self.amount <= 0:
            raise ValueError("充值数量必须大于0")


class UserDeductRequest(BaseModel):
    amount: int

    def model_post_init(self, __context):
        if self.amount <= 0:
            raise ValueError("扣除数量必须大于0")


# ---- Routes ----
@router.get("/users")
async def list_users(admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    # 批量查询：用户 + 令牌数 + 分组 — 消除 N+1
    users_result = await db.execute(select(User).order_by(User.id))
    users_list = list(users_result.scalars().all())
    user_ids = [u.id for u in users_list]

    # 批量查 token 数量
    token_counts = {}
    if user_ids:
        tc_result = await db.execute(
            select(Token.user_id, func.count(Token.id)).group_by(Token.user_id).where(Token.user_id.in_(user_ids))
        )
        token_counts = dict(tc_result.all())

    # 批量查分组
    group_ids = list(set(u.group_id for u in users_list if u.group_id))
    group_map = {}
    if group_ids:
        g_result = await db.execute(select(Group).where(Group.id.in_(group_ids)))
        group_map = {g.id: g for g in g_result.scalars().all()}

    # 预计算每个分组的授权模型（使用公共函数）
    group_authed_cache = {}
    for g in group_map.values():
        if g.id not in group_authed_cache:
            group_authed_cache[g.id] = await get_group_authed_models(db, g.id)

    out = []
    for u in users_list:
        grp = group_map.get(u.group_id) if u.group_id else None
        authed_models = group_authed_cache.get(grp.id, []) if grp else []
        out.append({
            "id": u.id, "username": u.username, "role": u.role,
            "group_id": u.group_id, "group_name": grp.name if grp else "",
            "enabled": u.enabled, "max_tokens": u.max_tokens,
            "token_quota": safe_int(u.token_quota), "token_quota_used": safe_int(u.token_quota_used),
            "allowed_models": u.allowed_models, "authed_models": authed_models,
            "token_count": token_counts.get(u.id, 0),
            "created_at": u.created_at.isoformat() if u.created_at else None
        })
    return out


@router.put("/users/{user_id}")
async def update_user(user_id: int, data: UserUpdate, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    # 超级管理员保护：非超管不能修改超管
    is_super_admin = admin.get("is_super", False)
    if user_id == SUPER_ADMIN_ID and not is_super_admin:
        raise HTTPException(403, "无法修改超级管理员")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if data.password:
        if len(data.password) < settings.MIN_PASSWORD_LENGTH:
            raise HTTPException(400, f"密码至少 {settings.MIN_PASSWORD_LENGTH} 个字符")
        if not re.search(r"[A-Za-z]", data.password) or not re.search(r"\d", data.password):
            raise HTTPException(400, "密码必须包含字母和数字")
        user.password_hash = hash_password(data.password)
    if data.enabled is not None: user.enabled = data.enabled
    if data.max_tokens is not None: user.max_tokens = data.max_tokens
    if data.token_quota is not None:
        if data.token_quota < -1:
            raise HTTPException(400, "token_quota 只能为 -1(无限) 或正数")
        user.token_quota = data.token_quota
    if data.token_quota_used is not None:
        if data.token_quota_used < 0:
            raise HTTPException(400, "token_quota_used 不能为负数")
        user.token_quota_used = data.token_quota_used
    if data.allowed_models is not None: user.allowed_models = data.allowed_models
    if data.group_id is not None: user.group_id = data.group_id if data.group_id > 0 else None
    if data.role is not None:
        if data.role not in ("admin", "operator", "user"):
            raise HTTPException(400, "Invalid role, must be admin/operator/user")
        # 超管角色不可被任何人修改（含超管自己也不能降级）
        if user.id == SUPER_ADMIN_ID and data.role != "admin":
            raise HTTPException(400, "无法更改超级管理员角色")
        # 非超管不能把用户提升为 admin 角色
        if data.role == "admin" and not is_super_admin:
            raise HTTPException(403, "只有超级管理员才能指定 admin 角色")
        # 非超管不能修改其他管理元的角色
        if user.role == "admin" and data.role != "admin" and not is_super_admin:
            raise HTTPException(400, "无法更改其他管理员角色")
        user.role = data.role
    await db.commit()
    return {"success": True}


@router.post("/users/{user_id}/recharge")
async def recharge_user(user_id: int, data: UserRechargeRequest, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.token_quota == -1:
        raise HTTPException(400, "用户额度为无限，无需充值")
    user.token_quota += data.amount
    await db.commit()
    await db.refresh(user)
    return {"success": True, "token_quota": safe_int(user.token_quota), "token_quota_used": safe_int(user.token_quota_used)}


@router.post("/users/{user_id}/deduct")
async def deduct_user(user_id: int, data: UserDeductRequest, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.token_quota == -1:
        raise HTTPException(400, "用户额度为无限，无法扣除")
    if user.token_quota - data.amount < user.token_quota_used:
        raise HTTPException(400, "扣除后额度不能低于已用量")
    user.token_quota -= data.amount
    await db.commit()
    await db.refresh(user)
    return {"success": True, "token_quota": safe_int(user.token_quota), "token_quota_used": safe_int(user.token_quota_used)}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    # 超级管理员绝对不可删除
    if user_id == SUPER_ADMIN_ID:
        raise HTTPException(403, "无法删除超级管理员")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    # 非超管不能删除其他管理员
    if user.role == "admin" and not admin.get("is_super", False):
        raise HTTPException(403, "无法删除管理员用户")
    result = await db.execute(select(Token).where(Token.user_id == user_id))
    for tk in result.scalars().all():
        await db.delete(tk)
    await db.delete(user)
    await db.commit()
    return {"success": True}
