#!/usr/bin/env python3
"""Restore the pre-upgrade SQLite snapshot while retaining the failed DB copy."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from pathlib import Path


def _backup(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {target_path.name}: {result}")
    finally:
        target.close()
        source.close()
    target_path.chmod(0o600)


def restore_database(database_path: Path, backup_path: Path, failed_copy_path: Path) -> None:
    database_path = Path(database_path)
    backup_path = Path(backup_path)
    failed_copy_path = Path(failed_copy_path)
    if not database_path.is_file() or not backup_path.is_file():
        raise FileNotFoundError("active database or pre-upgrade backup is missing")
    if failed_copy_path.exists():
        raise FileExistsError(f"failed-upgrade database copy already exists: {failed_copy_path.name}")

    original_stat = database_path.stat()
    _backup(database_path, failed_copy_path)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.restore-", suffix=".sqlite3", dir=database_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _backup(backup_path, temporary_path)
        os.chown(temporary_path, original_stat.st_uid, original_stat.st_gid)
        temporary_path.chmod(stat.S_IMODE(original_stat.st_mode))
        # The service has been stopped by the caller. Preserve its complete current state
        # above, then remove stale WAL sidecars before atomically replacing the main file.
        Path(f"{database_path}-wal").unlink(missing_ok=True)
        Path(f"{database_path}-shm").unlink(missing_ok=True)
        os.replace(temporary_path, database_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("pre_upgrade_backup", type=Path)
    parser.add_argument("failed_database_copy", type=Path)
    args = parser.parse_args()
    restore_database(args.database, args.pre_upgrade_backup, args.failed_database_copy)
    print(f"已保留升级失败时数据库副本：{args.failed_database_copy}")
    print(f"已恢复升级前数据库：{args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
