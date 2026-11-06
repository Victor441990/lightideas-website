"""Security helpers for the private Light Ideas administrator area."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
import secrets
import time

from flask import abort, request, session


def csrf_token() -> str:
    """Return a stable per-session token, creating it when needed."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf() -> None:
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    expected = session.get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400, description="Your administrator session has expired. Refresh the page and try again.")


@dataclass(frozen=True)
class LoginLimit:
    attempts: int = 5
    window_seconds: int = 15 * 60
    lockout_seconds: int = 15 * 60


class LoginRateLimiter:
    """Small, bounded, process-local login limiter.

    Render currently runs this app as one Gunicorn process. If that topology
    changes to multiple workers or instances, replace this with shared storage.
    """

    def __init__(self, limit: LoginLimit | None = None):
        self.limit = limit or LoginLimit()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._lock = Lock()

    def is_locked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            until = self._locked_until.get(key, 0)
            if until > now:
                return True
            self._locked_until.pop(key, None)
            return False

    def register_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= now - self.limit.window_seconds:
                attempts.popleft()
            attempts.append(now)
            if len(attempts) >= self.limit.attempts:
                self._locked_until[key] = now + self.limit.lockout_seconds
                attempts.clear()

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._locked_until.pop(key, None)


def client_address() -> str:
    """Use the forwarded client address after Flask ProxyFix has been enabled."""
    return request.remote_addr or "unknown"
