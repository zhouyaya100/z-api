"""Z API - 通知管理路由"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_, cast, Date
from pydantic import BaseModel
from datetime import datetime

from ..database import get_db
from ..models.notification import Notification
from ..models.user import User
from .auth import get_current_user, require_admin_by_token

router = APIRouter(prefix="/api", tags=["通知中心"])


# ---- Schemas ----
class NotificationCreate(BaseModel):
    title: str
    content: str = ""
    category: str = "info"          # fault / info
    receiver_id: int | None = None  # null=广播所有人


class NotificationBatchCreate(BaseModel):
    title: str
    content: str = ""
    category: str = "info"
    receiver_ids: list[int]         # 批量接收者


# ---- Routes ----

@router.get("/notifications")
async def list_notifications(
    category: str = "",
    read: str = "",   # "true"/"false"/""
    limit: int = 50,
    offset: int = 0,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户的通知列表（含广播 + 个人）"""
    user = await get_current_user(authorization, db)
    if not user:
        raise HTTPException(401, "Unauthorized")

    conditions = [Notification.receiver_id == user.id]
    # 管理员也能看到 sender_id 为空的系统通知（兼容旧广播记录）
    if user.role == "admin":
        conditions = [(Notification.receiver_id == user.id) | (Notification.receiver_id == None)]
    if category:
        conditions.append(Notification.category == category)
    if read == "true":
        conditions.append(Notification.read == True)
    elif read == "false":
        conditions.append(Notification.read == False)

    where = and_(*conditions)

    # Total count
    total_result = await db.execute(select(func.count()).select_from(Notification).where(where))
    total = total_result.scalar() or 0

    # Items
    result = await db.execute(
        select(Notification)
        .where(where)
        .order_by(Notification.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = result.scalars().all()

    # Batch lookup sender names
    sender_ids = list(set(n.sender_id for n in items if n.sender_id))
    sender_map = {}
    if sender_ids:
        r = await db.execute(select(User.id, User.username).where(User.id.in_(sender_ids)))
        sender_map = dict(r.all())

    return {
        "total": total,
        "items": [{
            "id": n.id,
            "category": n.category,
            "title": n.title,
            "content": n.content,
            "sender_id": n.sender_id,
            "sender_name": sender_map.get(n.sender_id) if n.sender_id else None,
            "receiver_id": n.receiver_id,
            "read": n.read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        } for n in items],
    }


@router.get("/notifications/sent")
async def list_sent_notifications(
    category: str = "",
    limit: int = 50,
    offset: int = 0,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """管理员查看已发送通知（按 sender_id 分组去重展示）"""
    admin = await require_admin_by_token(authorization, db)
    admin_id = admin.get('user_id') if isinstance(admin, dict) else None
    if not admin_id:
        admin_id = 1  # Admin static token fallback

    conditions = [Notification.sender_id == admin_id]
    if category:
        conditions.append(Notification.category == category)
    where = and_(*conditions)

    # 去重：相同 title+content+category+created_at 日期只显示一条（广播发多条合并展示）
    # 用子查询按 (title, content, category, date) 分组取 min(id)
    from sqlalchemy import func as safunc, cast, Date
    subq = (
        select(
            Notification.title,
            Notification.content,
            Notification.category,
            cast(Notification.created_at, Date).label('created_date'),
            safunc.min(Notification.id).label('represent_id'),
            safunc.count(Notification.id).label('recipient_count'),
        )
        .where(where)
        .group_by(Notification.title, Notification.content, Notification.category, cast(Notification.created_at, Date))
        .subquery()
    )

    # 总数
    total_result = await db.execute(select(safunc.count()).select_from(subq))
    total = total_result.scalar() or 0

    # 取代表记录
    result = await db.execute(
        select(Notification)
        .where(Notification.id.in_(select(subq.c.represent_id)))
        .order_by(Notification.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = result.scalars().all()

    # 同时获取每条的 recipient_count
    count_map = {}
    cr = await db.execute(select(subq.c.represent_id, subq.c.recipient_count))
    for row in cr.all():
        count_map[row[0]] = row[1]

    # Batch lookup sender names
    sender_ids = list(set(n.sender_id for n in items if n.sender_id))
    sender_map = {}
    if sender_ids:
        sr = await db.execute(select(User.id, User.username).where(User.id.in_(sender_ids)))
        sender_map = dict(sr.all())

    return {
        "total": total,
        "items": [{
            "id": n.id,
            "category": n.category,
            "title": n.title,
            "content": n.content,
            "sender_id": n.sender_id,
            "sender_name": sender_map.get(n.sender_id) if n.sender_id else None,
            "recipient_count": count_map.get(n.id, 1),
            "created_at": n.created_at.isoformat() if n.created_at else None,
        } for n in items],
    }


@router.delete("/notifications/sent/{nid}")
async def delete_sent_notification(
    nid: int,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """管理员删除已发送通知（删除该条及所有同批次的副本）"""
    admin = await require_admin_by_token(authorization, db)
    n = await db.get(Notification, nid)
    if not n:
        raise HTTPException(404, "Notification not found")
    # 删除同一批次（同 title + content + sender + 同一天）的所有记录
    from sqlalchemy import cast, Date
    await db.execute(
        Notification.__table__.delete().where(
            and_(
                Notification.sender_id == n.sender_id,
                Notification.title == n.title,
                Notification.content == n.content,
                Notification.category == n.category,
                cast(Notification.created_at, Date) == cast(n.created_at, Date),
            )
        )
    )
    await db.commit()
    return {"success": True}


@router.get("/notifications/unread_count")
async def unread_count(
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """获取未读通知数"""
    user = await get_current_user(authorization, db)
    if not user:
        raise HTTPException(401, "Unauthorized")

    result = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            and_(
                Notification.receiver_id == user.id,
                Notification.read == False,
            )
        )
    )
    return {"count": result.scalar() or 0}


@router.post("/notifications")
async def create_notification(
    data: NotificationCreate,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """管理员发送通知（单人或广播）"""
    admin = await require_admin_by_token(authorization, db)
    admin_id = admin.get('user_id') if isinstance(admin, dict) else None
    # Admin static token has no user_id, use super admin ID=1 as fallback
    if not admin_id:
        admin_id = 1
    if data.receiver_id is not None:
        # 发给指定用户（排除自己）
        if data.receiver_id != admin_id:
            n = Notification(
                category=data.category,
                title=data.title,
                content=data.content,
                sender_id=admin_id,
                receiver_id=data.receiver_id,
            )
            db.add(n)
    else:
        # 广播：给所有用户各发一条（排除自己）
        result = await db.execute(select(User.id).where(User.enabled == True, User.id != admin_id))
        user_ids = [row[0] for row in result.all()]
        for uid in user_ids:
            n = Notification(
                category=data.category,
                title=data.title,
                content=data.content,
                sender_id=admin_id,
                receiver_id=uid,
            )
            db.add(n)
    await db.commit()
    return {"success": True}


@router.post("/notifications/batch")
async def batch_create_notification(
    data: NotificationBatchCreate,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """管理员批量发送通知"""
    admin = await require_admin_by_token(authorization, db)
    admin_id = admin.get('user_id') if isinstance(admin, dict) else None
    if not admin_id:
        admin_id = 1
    for rid in data.receiver_ids:
        if rid == admin_id:
            continue  # 排除自己
        n = Notification(
            category=data.category,
            title=data.title,
            content=data.content,
            sender_id=admin_id,
            receiver_id=rid,
        )
        db.add(n)
    await db.commit()
    return {"success": True, "count": len(data.receiver_ids)}


@router.put("/notifications/{nid}/read")
async def mark_read(
    nid: int,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """标记通知为已读"""
    user = await get_current_user(authorization, db)
    if not user:
        raise HTTPException(401, "Unauthorized")
    n = await db.get(Notification, nid)
    if not n:
        raise HTTPException(404, "Notification not found")
    if n.receiver_id != user.id and user.role != "admin":
        raise HTTPException(403, "Not your notification")
    n.read = True
    await db.commit()
    return {"success": True}


@router.put("/notifications/read_all")
async def mark_all_read(
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """标记当前用户所有通知为已读"""
    user = await get_current_user(authorization, db)
    if not user:
        raise HTTPException(401, "Unauthorized")
    await db.execute(
        update(Notification)
        .where(
            and_(
                Notification.receiver_id == user.id,
                Notification.read == False,
            )
        )
        .values(read=True)
    )
    await db.commit()
    return {"success": True}


@router.delete("/notifications/{nid}")
async def delete_notification(
    nid: int,
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    """删除通知（管理员可删任何，用户只能删自己的）"""
    user = await get_current_user(authorization, db)
    if not user:
        raise HTTPException(401, "Unauthorized")
    n = await db.get(Notification, nid)
    if not n:
        raise HTTPException(404, "Notification not found")
    if user.role != "admin" and n.receiver_id != user.id:
        raise HTTPException(403, "Not your notification")
    await db.delete(n)
    await db.commit()
    return {"success": True}
