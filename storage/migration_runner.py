from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import asyncpg


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    path: Path
    checksum: str

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    root = directory or Path(__file__).with_name("sql")
    migrations: list[Migration] = []
    for path in sorted(root.glob("*.sql")):
        version = path.stem.split("_", maxsplit=1)[0]
        content = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                path=path,
                checksum=hashlib.sha256(content).hexdigest(),
            )
        )
    return migrations


async def run_migrations(pool: asyncpg.Pool) -> None:
    migrations = discover_migrations()
    async with pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        rows = await connection.fetch(
            "SELECT version, checksum FROM schema_migrations"
        )
        applied = {str(row["version"]): str(row["checksum"]) for row in rows}

        for migration in migrations:
            prior_checksum = applied.get(migration.version)
            if prior_checksum is not None:
                if prior_checksum != migration.checksum:
                    raise RuntimeError(
                        "migration checksum changed after application: "
                        f"{migration.version}"
                    )
                continue

            async with connection.transaction():
                await connection.execute(migration.sql)
                await connection.execute(
                    """
                    INSERT INTO schema_migrations(version, checksum)
                    VALUES($1, $2)
                    """,
                    migration.version,
                    migration.checksum,
                )
