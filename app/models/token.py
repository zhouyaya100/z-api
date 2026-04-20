"""Z API - 令牌模型"""
from sqlalchemy import Column, Integer, BigInteger, String, Boolean, Text, DateTime, ForeignKey
from datetime import datetime
from ..database.base import Base


class Token(Base):
    """访问令牌 (分发给用户)"""
    __tablename__ = "tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    name = Column(String, nullable=False)
    key = Column(String, unique=True, index=True, nullable=False)
    models = Column(Text, default="")                      # 令牌级模型限制 (空=继承用户)
    quota_limit = Column(BigInteger, default=-1)              # -1=无限
    quota_used = Column(BigInteger, default=0)
    enabled = Column(Boolean, default=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
