"""Z API - 日志模型"""
from sqlalchemy import Column, Integer, BigInteger, String, Boolean, Text, DateTime
from datetime import datetime
from ..database.base import Base


class Log(Base):
    """请求日志"""
    __tablename__ = "logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=0, index=True)
    token_id = Column(Integer, index=True)
    token_name = Column(String, default="")
    channel_id = Column(Integer, index=True)
    channel_name = Column(String, default="")
    model = Column(String, index=True)
    is_stream = Column(Boolean, default=False)
    prompt_tokens = Column(BigInteger, default=0)
    completion_tokens = Column(BigInteger, default=0)
    latency_ms = Column(Integer, default=0)
    success = Column(Boolean, default=True)
    error_msg = Column(Text, default="")
    client_ip = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
