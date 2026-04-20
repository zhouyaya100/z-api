"""Z API - 授权模型计算（公共函数）

消除 list_users / user_dashboard / my_available_models 三处重复逻辑
优先使用 channel_pool 倒排索引 O(1) 查询，fallback 到 DB 全表扫描
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models import Channel, Group
from ..core.routing.channel_pool import channel_pool


async def get_group_authed_models(db: AsyncSession, group_id: int | None, user_allowed_models: str = "") -> list[str]:
    """计算用户分组可用的授权模型列表

    Args:
        db: 数据库会话
        group_id: 用户分组 ID
        user_allowed_models: 用户级模型限制（空=全部）
    Returns:
        排序后的模型名列表
    """
    if not group_id:
        return []

    # 优先从 channel_pool 倒排索引 O(1) 获取
    grp = await db.get(Group, group_id)
    if not grp:
        return []

    models = channel_pool.get_models_for_group(grp.name)

    # Fallback: pool 为空时（启动初期或异常）走 DB 查询
    if not models:
        ch_result = await db.execute(select(Channel).where(Channel.enabled == True))
        models = []
        for ch in ch_result.scalars().all():
            if ch.allowed_groups:
                allowed_groups = [g.strip() for g in ch.allowed_groups.split(",") if g.strip()]
                if allowed_groups and grp.name not in allowed_groups:
                    continue
            if ch.models:
                for m in ch.models.split(","):
                    m = m.strip()
                    if m:
                        models.append(m)
            else:
                if ch.name:
                    models.append(ch.name)
        models = sorted(set(models))

    # 用户级模型过滤
    if user_allowed_models:
        user_models = set(m.strip() for m in user_allowed_models.split(",") if m.strip())
        models = [m for m in models if m in user_models]

    return models
