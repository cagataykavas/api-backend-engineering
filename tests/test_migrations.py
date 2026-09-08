from __future__ import annotations

import hashlib

from storage.migration_runner import discover_migrations


def test_migrations_are_discovered_in_version_order(tmp_path) -> None:
    second = tmp_path / "002_indexes.sql"
    first = tmp_path / "001_orders.sql"
    second.write_text("SELECT 2;\n", encoding="utf-8")
    first.write_text("SELECT 1;\n", encoding="utf-8")

    migrations = discover_migrations(tmp_path)

    assert [migration.version for migration in migrations] == ["001", "002"]
    assert migrations[0].checksum == hashlib.sha256(first.read_bytes()).hexdigest()


def test_repository_migration_set_is_not_empty() -> None:
    migrations = discover_migrations()
    assert migrations
    assert migrations[0].version == "001"
    assert migrations[0].path.name == "001_orders.sql"
