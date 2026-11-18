"""Idempotent schema migrations for the dev databases.

P0 used ``create_all``; adding a column later needs an explicit ALTER, and a
real deployment needs Alembic.  This keeps the local loop honest without
pretending to be a migration framework.

Usage:  python scripts/migrate.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from core.config import get_settings
from core.db import engine

PG_STATEMENTS = [
    "ALTER TABLE repos ADD COLUMN IF NOT EXISTS clone_url VARCHAR(512) NOT NULL DEFAULT ''",
]

SQLITE_STATEMENTS = [
    ("repos", "clone_url", "ALTER TABLE repos ADD COLUMN clone_url VARCHAR(512) NOT NULL DEFAULT ''"),
]


async def _sqlite_column_exists(conn, table: str, column: str) -> bool:
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
    return any(row[1] == column for row in rows)


async def main() -> int:
    settings = get_settings()
    async with engine.begin() as conn:
        if settings.is_sqlite:
            for table, column, ddl in SQLITE_STATEMENTS:
                if not await _sqlite_column_exists(conn, table, column):
                    await conn.execute(text(ddl))
                    print(f"sqlite: added {table}.{column}")
                else:
                    print(f"sqlite: {table}.{column} ok")
        else:
            for stmt in PG_STATEMENTS:
                await conn.execute(text(stmt))
                print(f"postgres: {stmt.split('ADD COLUMN')[1].split()[0]} ok")
    await engine.dispose()
    print("migrate done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
