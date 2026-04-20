"""Z API - 用户分组模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from ..database.base import Base


class Group(Base):
    """用户分组"""
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    comment = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
