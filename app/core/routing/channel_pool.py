"""Z API - 渠道池 (倒排索引 + 增量更新)

核心优化：model → priority → [channels] 倒排索引
- 请求查找 O(1) 而非 O(n) 全表扫描
- 分组预计算，不用每次 split 字符串
- 管理员改渠道只更新索引项，不全清
"""
import random
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("z-api")


@dataclass
class ChannelInfo:
    """渠道轻量信息（不存 api_key 等敏感字段）"""
    id: int
    name: str
    type: str
    base_url: str
    api_key: str
    models: list[str]
    model_mapping: dict[str, str]
    allowed_groups: set[str]  # 空集=全部分组
    weight: int
    priority: int
    auto_ban: bool
    fail_count: int = 0

    # 运行时状态（非持久化）
    enabled: bool = True


class ChannelPool:
    """渠道池：倒排索引 + 分组预计算"""

    def __init__(self):
        # id → ChannelInfo
        self._channels: dict[int, ChannelInfo] = {}
        # 倒排索引: model → priority → [channel_id]
        self._index: dict[str, dict[int, list[int]]] = {}
        # 分组预计算: group_name → set(channel_id)
        self._group_channels: dict[str, set[int]] = {}
        # model → 所属 channel_ids (用于分组过滤加速)
        self._model_channels: dict[str, set[int]] = {}
        # 反向映射索引: 映射后模型名 → 映射前模型名 (用于权限检查)
        self._reverse_mapping: dict[str, str] = {}
        # 版本号（用于增量同步，预留 Redis pub/sub 场景）
        self._version: int = 0

    @property
    def version(self) -> int:
        return self._version

    def rebuild(self, channels: list):
        """全量重建索引（启动时或定期全量同步）

        Args:
            channels: Channel ORM 对象列表
        """
        self._channels.clear()
        self._index.clear()
        self._group_channels.clear()
        self._model_channels.clear()
        self._reverse_mapping.clear()

        for ch in channels:
            if not ch.enabled:
                continue
            info = self._orm_to_info(ch)
            self._channels[info.id] = info
            self._add_to_index(info)

        self._version += 1
        logger.info(f"ChannelPool rebuilt: {len(self._channels)} channels, "
                     f"{len(self._index)} models indexed, version={self._version}")

    def update_channel(self, channel):
        """增量更新单个渠道（管理操作触发）"""
        info = self._orm_to_info(channel)

        # 先移除旧索引
        if channel.id in self._channels:
            self._remove_from_index(self._channels[channel.id])

        if channel.enabled:
            self._channels[channel.id] = info
            self._add_to_index(info)
        else:
            self._channels.pop(channel.id, None)

        self._version += 1

    def remove_channel(self, channel_id: int):
        """移除渠道"""
        if channel_id in self._channels:
            self._remove_from_index(self._channels[channel_id])
            del self._channels[channel_id]
            self._version += 1

    def get(self, channel_id: int) -> Optional[ChannelInfo]:
        """获取渠道信息"""
        return self._channels.get(channel_id)

    def select(self, model: str, group: str | None = None,
               exclude_ids: set[int] | None = None) -> Optional[ChannelInfo]:
        """O(1) 选择渠道

        Args:
            model: 请求模型名
            group: 用户分组名 (None=无分组)
            exclude_ids: 重试排除的渠道 ID
        Returns:
            ChannelInfo or None
        """
        priorities = self._index.get(model)
        if not priorities:
            return None

        exclude = exclude_ids or set()

        # 从最高优先级往下找
        for pri in sorted(priorities.keys(), reverse=True):
            candidates = []
            for cid in priorities[pri]:
                ch = self._channels.get(cid)
                if not ch or not ch.enabled or cid in exclude:
                    continue
                # 分组过滤：group=None(无分组)时只匹配不限制分组的渠道(allowed_groups为空)
                # group有值时：allowed_groups为空=全部分组可用，否则必须在列表中
                if group is not None and ch.allowed_groups and group not in ch.allowed_groups:
                    continue
                if group is None and ch.allowed_groups:
                    continue
                candidates.append(ch)
            if candidates:
                weights = [max(c.weight, 1) for c in candidates]
                return random.choices(candidates, weights=weights, k=1)[0]

        return None

    def get_models_for_group(self, group: str | None) -> list[str]:
        """获取分组可用的模型列表（预计算）"""
        if not group:
            # 无分组：返回所有模型
            return sorted(self._index.keys())

        models = []
        for model, channel_ids in self._model_channels.items():
            for cid in channel_ids:
                ch = self._channels.get(cid)
                if ch and (not ch.allowed_groups or group in ch.allowed_groups):
                    models.append(model)
                    break
        return sorted(set(models))

    def update_fail_count(self, channel_id: int, fail_count: int, enabled: bool):
        """更新渠道失败计数和状态"""
        ch = self._channels.get(channel_id)
        if ch:
            ch.fail_count = fail_count
            if not enabled:
                ch.enabled = False
                self._remove_from_index(ch)
                # 注意：不从 _channels 移除，保留信息用于管理面板

    def all_channels(self) -> list[ChannelInfo]:
        """获取所有渠道信息（含禁用）"""
        return list(self._channels.values())

    def reverse_map(self, external_model: str) -> str | None:
        """反向映射：映射后名称 → 映射前名称（用于权限检查）
        返回 None 表示没有反向映射
        """
        return self._reverse_mapping.get(external_model)

    def _add_to_index(self, info: ChannelInfo):
        """将渠道加入倒排索引"""
        for model in info.models:
            # model → priority → [channel_id]
            if model not in self._index:
                self._index[model] = {}
            if info.priority not in self._index[model]:
                self._index[model][info.priority] = []
            self._index[model][info.priority].append(info.id)

            # model → channel_ids
            if model not in self._model_channels:
                self._model_channels[model] = set()
            self._model_channels[model].add(info.id)

            # 反向映射：映射后名称 → 映射前名称
            if info.model_mapping:
                for src, dst in info.model_mapping.items():
                    if dst != src:
                        self._reverse_mapping[dst] = src



        # group → channel_ids
        if info.allowed_groups:
            for group in info.allowed_groups:
                if group not in self._group_channels:
                    self._group_channels[group] = set()
                self._group_channels[group].add(info.id)
        else:
            # 空分组 = 全部可用，加入特殊 key
            if "*" not in self._group_channels:
                self._group_channels["*"] = set()
            self._group_channels["*"].add(info.id)

    def _remove_from_index(self, info: ChannelInfo):
        """将渠道从倒排索引移除"""
        for model in info.models:
            if model in self._index:
                for pri in self._index[model]:
                    if info.id in self._index[model][pri]:
                        self._index[model][pri].remove(info.id)
                # 清理空列表
                self._index[model] = {p: v for p, v in self._index[model].items() if v}
                if not self._index[model]:
                    del self._index[model]

            if model in self._model_channels:
                self._model_channels[model].discard(info.id)

        # 清理反向映射
        if info.model_mapping:
            for src, dst in info.model_mapping.items():
                if dst != src and self._reverse_mapping.get(dst) == src:
                    del self._reverse_mapping[dst]

        for group in list(self._group_channels.keys()):
            self._group_channels[group].discard(info.id)
            if not self._group_channels[group]:
                del self._group_channels[group]

    @staticmethod
    def _orm_to_info(ch) -> ChannelInfo:
        """Channel ORM → ChannelInfo"""
        import json as _json
        models = [m.strip() for m in ch.models.split(",") if m.strip()] if ch.models else []
        mapping = {}
        if ch.model_mapping:
            try:
                mapping = _json.loads(ch.model_mapping)
            except:
                pass
        groups = set(g.strip() for g in ch.allowed_groups.split(",") if g.strip()) if ch.allowed_groups else set()

        return ChannelInfo(
            id=ch.id, name=ch.name, type=ch.type or "openai",
            base_url=ch.base_url, api_key=ch.api_key,
            models=models, model_mapping=mapping,
            allowed_groups=groups,
            weight=ch.weight or 1, priority=ch.priority or 0,
            auto_ban=ch.auto_ban if ch.auto_ban is not None else True,
            fail_count=ch.fail_count or 0,
            enabled=ch.enabled if ch.enabled is not None else True,
        )


# 全局实例
channel_pool = ChannelPool()
