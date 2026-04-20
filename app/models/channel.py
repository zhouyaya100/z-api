"""Z API - 渠道模型"""
from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime
from datetime import datetime
from ..database.base import Base


class Channel(Base):
    """API 渠道 (上游供应商)"""
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    type = Column(String, default="openai")
    base_url = Column(String, nullable=False)
    api_key = Column(Text, nullable=False)
    models = Column(Text, default="")
    model_mapping = Column(Text, default="")
    allowed_groups = Column(Text, default="")            # 允许使用的分组 (空=全部)
    weight = Column(Integer, default=1)
    priority = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)
    auto_ban = Column(Boolean, default=True)
    fail_count = Column(Integer, default=0)
    test_time = Column(DateTime, nullable=True)
    response_time = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
