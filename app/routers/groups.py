"""Z API - 分组管理路由"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from ..database import get_db
from ..models import Group, User
from .auth import require_admin_by_token, admin_auth

router = APIRouter(prefix="/api", tags=["分组管理"])



# ---- Schemas ----
class GroupCreate(BaseModel):
    name: str
    comment: str = ""


class GroupUpdate(BaseModel):
    name: str | None = None
    comment: str | None = None


# ---- Routes ----
@router.get("/groups")
async def list_groups(admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    # 批量查询：消除 N+1
    result = await db.execute(select(Group).order_by(Group.id))
    groups_list = list(result.scalars().all())
    group_ids = [g.id for g in groups_list]

    # 批量查每个分组的用户数
    user_counts = {}
    if group_ids:
        uc_result = await db.execute(
            select(User.group_id, func.count(User.id)).group_by(User.group_id).where(User.group_id.in_(group_ids))
        )
        user_counts = dict(uc_result.all())

    return [{"id": g.id, "name": g.name, "comment": g.comment,
             "user_count": user_counts.get(g.id, 0),
             "created_at": g.created_at.isoformat() if g.created_at else None} for g in groups_list]


@router.post("/groups")
async def create_group(data: GroupCreate, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(select(Group).where(Group.name == data.name))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, f"分组 '{data.name}' 已存在")
    g = Group(name=data.name, comment=data.comment)
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return {"success": True, "id": g.id}


@router.put("/groups/{group_id}")
async def update_group(group_id: int, data: GroupUpdate, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "Group not found")
    if data.name is not None:
        exists = (await db.execute(select(Group).where(Group.name == data.name, Group.id != group_id))).scalar_one_or_none()
        if exists:
            raise HTTPException(400, f"分组 '{data.name}' 已存在")
        g.name = data.name
    if data.comment is not None: g.comment = data.comment
    await db.commit()
    return {"success": True}


@router.delete("/groups/{group_id}")
async def delete_group(group_id: int, admin=Depends(admin_auth), db: AsyncSession = Depends(get_db)):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "Group not found")
    result = await db.execute(select(User).where(User.group_id == group_id))
    for u in result.scalars().all():
        u.group_id = None
    await db.delete(g)
    await db.commit()
    return {"success": True}
