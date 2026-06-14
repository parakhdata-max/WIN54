from __future__ import annotations

import os
from dotenv import load_dotenv


def _run(env_name: str) -> None:
    os.environ["APP_ENV"] = env_name
    from modules.db.migrations.runner import run_pending_migrations

    applied = run_pending_migrations()
    print(f"{env_name}: {applied or 'no pending migrations'}")


if __name__ == "__main__":
    load_dotenv(".env")
    _run("TEST")
    _run("PROD")
