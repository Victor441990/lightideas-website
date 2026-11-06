"""Environment-only configuration for Light Ideas Technology."""

from __future__ import annotations

import os
from datetime import timedelta


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_production() -> bool:
    return env_flag("RENDER") or os.environ.get("FLASK_ENV", "").lower() == "production"


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = env_flag("SESSION_COOKIE_SECURE", is_production())
    TRUST_PROXY_HEADERS = env_flag("TRUST_PROXY_HEADERS", is_production())
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(os.environ.get("ADMIN_SESSION_MINUTES", "60"))
    )
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    BIZTRACK_SERVICE_URL = os.environ.get("BIZTRACK_SERVICE_URL")
    BIZTRACK_INTEGRATION_TOKEN = os.environ.get("BIZTRACK_INTEGRATION_TOKEN")
