"""Runtime environment helpers for TEST vs LIVE database separation."""

from __future__ import annotations

import os
from urllib.parse import urlparse


def app_env() -> str:
    return os.getenv("APP_ENV", "TEST").strip().upper() or "TEST"


def is_prod() -> bool:
    return app_env() == "PROD"


def db_url() -> str:
    return os.getenv("DATABASE_PROD" if is_prod() else "DATABASE_TEST", "") or ""


def db_label(url: str | None = None) -> str:
    url = url or db_url()
    try:
        parsed = urlparse(url)
        return parsed.path.lstrip("/") or "unknown"
    except Exception:
        return "unknown"


def test_login_password() -> str:
    """Optional extra TEST-mode password.

    If TEST_LOGIN_PASSWORD is set, test-mode login requires this password
    instead of the normal user's DB password. PROD is never affected.
    """
    return os.getenv("TEST_LOGIN_PASSWORD", "").strip()

