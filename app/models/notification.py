"""Z API - 通知模型"""
from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime
from datetime import datetime
from ..database.base import Base


class Notification(Base):
    """系统通知"""
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(20), default="info")       # fault / info
    title = Column(String(200), nullable=False)
    content = Column(Text, default="")
    sender_id = Column(Integer, nullable=True)           # 发送者 ID (null=系统)
    receiver_id = Column(Integer, nullable=True)         # 接收者 ID (null=广播)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
