"""One-time, non-destructive import of a local LoCal install for the owner."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

from . import paths, tenancy


class ImportRefused(Exception):
    pass


def _owner() -> tenancy.TenantPaths:
    owners = [tenant for tenant in tenancy.listing() if tenant.role == "owner"]
    if len(owners) != 1:
        raise ImportRefused(
            "the hosted owner must complete one Cloudflare Access login before import"
        )
    return owners[0]


def _database_has_user_data(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    conn = sqlite3.connect(path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for table in ("source_post", "event", "account", "web_source", "trip", "spend"):
            if table in tables and conn.execute(
                    f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        return False
    finally:
        conn.close()


def _copy_directory(source: Path, destination: Path) -> int:
    if not source.exists():
        return 0
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = [path for path in source.rglob("*") if path.is_file()]
    collisions = [path for path in files
                  if (destination / path.relative_to(source)).exists()]
    if collisions:
        raise ImportRefused(
            f"destination already contains {len(collisions)} matching media file(s)"
        )
    for source_file in files:
        target = destination / source_file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source_file, target)
    return len(files)


def _collision_count(source: Path, destination: Path) -> int:
    if not source.exists():
        return 0
    return sum(
        1 for path in source.rglob("*")
        if path.is_file() and (destination / path.relative_to(source)).exists()
    )


def import_owner(source_root: Path, owner: tenancy.TenantPaths | None = None) -> dict:
    """Copy local data into an empty hosted owner tenant; never alter source."""
    source_root = Path(source_root).expanduser().resolve()
    source_db = source_root / "calendar.db"
    if not source_db.is_file():
        raise ImportRefused(f"no calendar.db found in {source_root}")

    owner = (owner or _owner()).ensure()
    if _database_has_user_data(owner.db_path):
        raise ImportRefused("hosted owner calendar already contains user data")
    for suffix in ("-wal", "-shm"):
        if Path(f"{owner.db_path}{suffix}").exists():
            raise ImportRefused("stop the hosted web service before importing")
    for source_dir, destination_dir in (
            (source_root / "media", owner.media_dir),
            (source_root / "avatars", owner.avatar_dir)):
        collisions = _collision_count(source_dir, destination_dir)
        if collisions:
            raise ImportRefused(
                f"destination already contains {collisions} matching media file(s)"
            )
    source_config = source_root / "config.json"
    if source_config.exists() and owner.config_path.exists():
        raise ImportRefused("hosted owner already has a config.json")

    temporary_db = owner.root / "calendar.importing.db"
    temporary_db.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    destination = sqlite3.connect(temporary_db)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    temporary_db.replace(owner.db_path)

    media_count = _copy_directory(source_root / "media", owner.media_dir)
    avatar_count = _copy_directory(source_root / "avatars", owner.avatar_dir)
    if source_config.exists():
        shutil.copy2(source_config, owner.config_path)

    return {
        "owner": owner.email,
        "database": str(owner.db_path),
        "media_files": media_count,
        "avatar_files": avatar_count,
        "config_copied": source_config.exists(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a local LoCal data directory into the hosted owner tenant"
    )
    parser.add_argument("source", nargs="?", default=str(paths.HOME))
    parser.add_argument(
        "--yes", action="store_true",
        help="perform the import; without this flag only the resolved paths are shown",
    )
    args = parser.parse_args()
    owner = _owner()
    source = Path(args.source).expanduser().resolve()
    print(f"source:      {source}")
    print(f"destination: {owner.root}")
    print(f"owner:       {owner.email}")
    if not args.yes:
        print("dry run only; add --yes after stopping the hosted web service")
        return
    try:
        result = import_owner(source, owner)
    except ImportRefused as exc:
        raise SystemExit(f"import refused: {exc}") from exc
    print(f"database imported; {result['media_files']} media and "
          f"{result['avatar_files']} avatar files copied")
    print("provider secrets were not copied; assign the owner's DeepSeek key in /admin/users")


if __name__ == "__main__":
    main()
