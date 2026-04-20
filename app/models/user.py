"""Z API - 用户模型"""
from sqlalchemy import Column, Integer, BigInteger, String, Boolean, Text, DateTime, ForeignKey
from datetime import datetime
from ..database.base import Base


class User(Base):
    """用户"""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")                  # admin / operator / user
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True, index=True)
    enabled = Column(Boolean, default=True)
    max_tokens = Column(Integer, default=3)                # 最多可创建令牌数
    token_quota = Column(BigInteger, default=-1)              # Token用量总配额 (-1=无限)
    token_quota_used = Column(BigInteger, default=0)          # Token用量已用
    allowed_models = Column(Text, default="")              # 允许使用的模型 (空=全部)
    created_at = Column(DateTime, default=datetime.utcnow)
