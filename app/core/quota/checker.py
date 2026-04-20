"""Z API - 配额检查器

职责：验证令牌和用户的配额状态
设计：纯检查，不修改数据；预留 Redis 后端接口
"""
import time
import logging
from fastapi import HTTPException

from ...models import Token, User
from .types import QuotaResult, QuotaStatus

logger = logging.getLogger("z-api")


class QuotaChecker:
    """配额检查器

    当前实现：直接查 DB
    未来可替换为 Redis 后端：重写 _get_token_quota / _get_user_quota
    """

    async def check_token(self, token: Token, model: str | None = None) -> QuotaResult:
        """检查令牌配额和状态"""
        # 1. 令牌过期
        if token.expires_at and token.expires_at.timestamp() < time.time():
            return QuotaResult(
                status=QuotaStatus.TOKEN_EXPIRED,
                token_id=token.id, user_id=token.user_id,
                token_quota_limit=token.quota_limit, token_quota_used=token.quota_used,
                message="API Key 已过期",
            )
        # 2. 令牌配额
        if token.quota_limit != -1 and token.quota_used >= token.quota_limit:
            return QuotaResult(
                status=QuotaStatus.TOKEN_QUOTA_EXCEEDED,
                token_id=token.id, user_id=token.user_id,
                token_quota_limit=token.quota_limit, token_quota_used=token.quota_used,
                message="令牌额度不足，请联系管理员充值",
            )
        return QuotaResult(
            status=QuotaStatus.OK,
            token_id=token.id, user_id=token.user_id,
            token_quota_limit=token.quota_limit, token_quota_used=token.quota_used,
        )

    async def check_user(self, user: User) -> QuotaResult:
        """检查用户配额和状态"""
        # 1. 用户禁用
        if not user.enabled:
            return QuotaResult(
                status=QuotaStatus.USER_DISABLED,
                token_id=0, user_id=user.id,
                token_quota_limit=0, token_quota_used=0,
                user_quota_limit=user.token_quota, user_quota_used=user.token_quota_used,
                message="用户已被禁用",
            )
        # 2. 用户配额
        if user.token_quota != -1 and user.token_quota_used >= user.token_quota:
            return QuotaResult(
                status=QuotaStatus.USER_QUOTA_EXCEEDED,
                token_id=0, user_id=user.id,
                token_quota_limit=0, token_quota_used=0,
                user_quota_limit=user.token_quota, user_quota_used=user.token_quota_used,
                message="用户额度不足，请联系管理员充值",
            )
        return QuotaResult(
            status=QuotaStatus.OK,
            token_id=0, user_id=user.id,
            token_quota_limit=0, token_quota_used=0,
            user_quota_limit=user.token_quota, user_quota_used=user.token_quota_used,
        )

    def raise_if_failed(self, result: QuotaResult):
        """配额检查失败时抛出 HTTPException (OpenAI 格式)"""
        if result.ok:
            return
        error_map = {
            QuotaStatus.TOKEN_EXPIRED: (401, "invalid_api_key", result.message),
            QuotaStatus.TOKEN_QUOTA_EXCEEDED: (429, "quota_exceeded", result.message),
            QuotaStatus.USER_DISABLED: (401, "user_disabled", result.message),
            QuotaStatus.USER_QUOTA_EXCEEDED: (429, "quota_exceeded", result.message),
        }
        status_code, code, msg = error_map.get(result.status, (403, "quota_error", result.message))
        raise HTTPException(status_code, detail={
            "error": {"message": msg, "type": "insufficient_quota", "code": code}
        })


# 全局实例
quota_checker = QuotaChecker()
