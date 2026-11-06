"""Run the Light Ideas admin locally with an in-memory MongoDB substitute.

This is deliberately guarded so it cannot be used as a production entrypoint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import mongomock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.environ.get("LIGHTIDEAS_LOCAL_DEMO") != "1":
    raise SystemExit("Set LIGHTIDEAS_LOCAL_DEMO=1 to run the local demonstration.")

if not os.environ.get("BIZTRACK_SERVICE_URL", "").startswith("http://127.0.0.1:"):
    raise SystemExit("The local demonstration requires a loopback Biztrack service URL.")

import app as application

application._mongo_client = mongomock.MongoClient()
application.app.run(host="127.0.0.1", port=5050, debug=False)
