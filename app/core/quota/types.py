"""Z API - 配额类型定义"""
from enum import Enum
from dataclasses import dataclass


class QuotaStatus(str, Enum):
    """配额检查结果状态"""
    OK = "ok"
    TOKEN_QUOTA_EXCEEDED = "token_quota_exceeded"
    USER_QUOTA_EXCEEDED = "user_quota_exceeded"
    TOKEN_EXPIRED = "token_expired"
    USER_DISABLED = "user_disabled"


@dataclass
class QuotaResult:
    """配额检查结果"""
    status: QuotaStatus
    token_id: int
    user_id: int | None
    token_quota_limit: int
    token_quota_used: int
    user_quota_limit: int | None = None
    user_quota_used: int | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == QuotaStatus.OK
