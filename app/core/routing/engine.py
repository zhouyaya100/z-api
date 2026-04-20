"""Z API - 路由引擎

职责：串联渠道池 + 策略，提供统一的路由入口
"""
import json
import logging
from fastapi import HTTPException

from .channel_pool import ChannelPool, channel_pool
from .policy import RoutingPolicy, RoutingStrategy, RoutingContext, routing_policy
from ...models import User

logger = logging.getLogger("z-api")


class RoutingEngine:
    """路由引擎：模型映射 + 渠道选择 + 分组策略"""

    def __init__(self, pool: ChannelPool = None, policy: RoutingPolicy = None):
        self.pool = pool or channel_pool
        self.policy = policy or routing_policy

    def resolve_model(self, channel_info, model: str) -> str:
        """模型映射：将请求模型名映射为渠道实际模型名"""
        if channel_info.model_mapping:
            return channel_info.model_mapping.get(model, model)
        return model

    def select_channel(self, model: str, user: User = None,
                       group_name: str | None = None,
                       exclude_ids: set[int] | None = None,
                       strategy: RoutingStrategy = RoutingStrategy.PRIORITY_WEIGHTED) -> object:
        """选择渠道

        Args:
            model: 请求模型名
            user: 用户对象 (可选)
            group_name: 用户分组名 (可选，优先于 user.group 查询)
            exclude_ids: 重试排除的渠道 ID
            strategy: 路由策略
        Returns:
            ChannelInfo
        Raises:
            HTTPException: 无分组 / 无可用渠道
        """
        # 获取分组名
        if group_name is None and user and user.group_id:
            # 需要从外部传入 group_name，这里不做 DB 查询
            pass

        # 检查分组：无分组用户只允许不限制分组的渠道（group=None 在 pool.select 中自动过滤）
        # 不再直接 403，让 pool.select 决定是否有可用渠道

        # 从渠道池选择
        channel_info = self.pool.select(model, group=group_name, exclude_ids=exclude_ids)
        if not channel_info:
            raise HTTPException(404, detail={
                "error": {"message": f"模型 '{model}' 没有可用渠道",
                          "type": "invalid_request_error", "code": "model_not_found"}
            })

        # 策略选择（如果有多个候选，已在 pool.select 中处理）
        return channel_info

    def build_upstream_url(self, channel_info, request_path: str) -> str:
        """构建上游 URL"""
        base = channel_info.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base + request_path.replace("/v1", "", 1)
        return base + request_path

    def build_headers(self, channel_info, request_headers: dict = None) -> dict:
        """构建上游请求头"""
        headers = {
            "Authorization": f"Bearer {channel_info.api_key}",
            "Content-Type": "application/json",
        }
        if request_headers:
            for h in ["Accept", "User-Agent"]:
                if h in request_headers:
                    headers[h] = request_headers[h]
        return headers


# 全局实例
routing_engine = RoutingEngine()
