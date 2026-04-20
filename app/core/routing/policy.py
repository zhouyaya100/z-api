"""Z API - 路由策略

职责：决定从候选渠道中选择哪个
设计：策略模式，支持优先级权重 / 轮询 / 最低延迟 / 成本优先
"""
import random
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Optional

from .channel_pool import ChannelInfo

logger = logging.getLogger("z-api")


class RoutingStrategy(str, Enum):
    """路由策略枚举"""
    PRIORITY_WEIGHTED = "priority_weighted"      # 优先级 + 权重随机（当前默认）
    ROUND_ROBIN = "round_robin"                  # 轮询
    LEAST_LATENCY = "least_latency"              # 最低延迟优先
    COST_OPTIMIZED = "cost_optimized"            # 成本优先（需渠道配置成本权重）


@dataclass
class RoutingContext:
    """路由上下文"""
    model: str
    group: str | None = None
    user_id: int | None = None
    is_stream: bool = False
    exclude_channel_ids: set[int] | None = None


class RoutingPolicy:
    """路由策略选择器"""

    def __init__(self):
        self._strategies = {
            RoutingStrategy.PRIORITY_WEIGHTED: self._priority_weighted,
            RoutingStrategy.ROUND_ROBIN: self._round_robin,
            RoutingStrategy.LEAST_LATENCY: self._least_latency,
        }
        # 轮询计数器: model → index
        self._rr_counter: dict[str, int] = {}

    def select(self, candidates: list[ChannelInfo],
               strategy: RoutingStrategy = RoutingStrategy.PRIORITY_WEIGHTED,
               context: RoutingContext | None = None) -> Optional[ChannelInfo]:
        """根据策略选择渠道"""
        if not candidates:
            return None
        handler = self._strategies.get(strategy, self._priority_weighted)
        return handler(candidates, context)

    def _priority_weighted(self, candidates: list[ChannelInfo],
                           context: RoutingContext | None = None) -> ChannelInfo:
        """优先级 + 权重随机"""
        # 按优先级分组
        max_priority = max(c.priority for c in candidates)
        top = [c for c in candidates if c.priority == max_priority]
        weights = [max(c.weight, 1) for c in top]
        return random.choices(top, weights=weights, k=1)[0]

    def _round_robin(self, candidates: list[ChannelInfo],
                     context: RoutingContext | None = None) -> ChannelInfo:
        """轮询（按模型维度）"""
        model = context.model if context else ""
        idx = self._rr_counter.get(model, 0)
        selected = candidates[idx % len(candidates)]
        self._rr_counter[model] = idx + 1
        return selected

    def _least_latency(self, candidates: list[ChannelInfo],
                       context: RoutingContext | None = None) -> ChannelInfo:
        """最低延迟优先（基于 response_time）"""
        # ChannelInfo 没有 response_time，用 fail_count 近似
        # TODO: 在 ChannelInfo 中增加 response_time 字段
        return min(candidates, key=lambda c: c.fail_count)


# 全局实例
routing_policy = RoutingPolicy()
