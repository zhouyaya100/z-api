"""Z API - Routers Package"""
from .auth import router as auth_router
from .channels import router as channels_router
from .users import router as users_router
from .tokens import router as tokens_router
from .logs import router as logs_router
from .groups import router as groups_router
from .settings import router as settings_router
from .stats import router as stats_router
from .reports import router as reports_router
from .notifications import router as notifications_router

__all__ = [
    "auth_router", "channels_router", "users_router", "tokens_router",
    "logs_router", "groups_router", "settings_router", "stats_router",
    "reports_router", "notifications_router",
]
