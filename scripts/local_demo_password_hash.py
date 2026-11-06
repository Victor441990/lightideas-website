"""Generate a temporary demo password hash from process memory only."""

from __future__ import annotations

import os
import sys

from werkzeug.security import generate_password_hash

if os.environ.get("LIGHTIDEAS_LOCAL_DEMO") != "1":
    raise SystemExit("This helper is only for the guarded local demonstration.")

password = os.environ.get("LIGHTIDEAS_DEMO_PASSWORD")
if not password:
    raise SystemExit("A temporary demo password is required.")

print(generate_password_hash(password))
