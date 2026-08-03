"""Isolate the test suite from the developer's local .env.

``config.py`` calls ``load_dotenv()``, which picks up whatever ``.env`` the
working copy happens to have. That made collection fail on any machine whose
``.env`` points ``XRAY_EXECUTABLE_PATH`` at a real install (importing
``app.xray`` shells out to ``xray version``), and it meant tests could touch
the developer's real database.

``load_dotenv()`` does not override variables that are already set, so setting
them here -- before pytest imports any test module, and therefore before
``config`` is first imported -- wins over ``.env``.
"""

import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_TEST_ENV = {
    # No local Xray process, so no dependency on an xray binary being present.
    "XRAY_LOCAL_CORE_ENABLED": "false",
    "XRAY_EXECUTABLE_PATH": str(REPO_ROOT / "tests" / "does-not-exist-xray"),
    "XRAY_ASSETS_PATH": str(REPO_ROOT / "tests"),
    "XRAY_JSON": str(REPO_ROOT / "xray_config.json"),
    "XRAY_JSON_DB_BACKED": "false",
    # Never point at whatever the developer is running locally.
    "SQLALCHEMY_DATABASE_URL": "sqlite:///"
    + os.path.join(tempfile.gettempdir(), "marzban-tests.sqlite3"),
    "HA_ENABLED": "false",
    "TELEGRAM_API_TOKEN": "",
    "WEBHOOK_ADDRESS": "",
    "DISCORD_WEBHOOK_URL": "",
    "SUDO_USERNAME": "",
    "SUDO_PASSWORD": "",
    "UVICORN_SSL_CERTFILE": "",
    "UVICORN_SSL_KEYFILE": "",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)
