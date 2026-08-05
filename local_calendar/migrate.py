"""One-time move of an in-tree install into the app's data directory.

Before `paths.py`, everything lived beside the checkout: `data/calendar.db`,
`data/media`, `data/avatars`, `config.json`, `.env.local`. This copies that
layout into `paths.HOME` so an editable install or a packaged .app finds it.

Two deliberate choices:

  * **Copies, never moves.** The media directory is 200MB+ and the database is
    the only record of everything this install has ever scraped. Leaving the
    originals in place means a failure here costs disk, not data. Delete the old
    `data/` by hand once the app looks right.
  * **Never overwrites.** Re-running is a no-op, so it is safe to call from a
    first-run path or to re-run after a partial copy.

`spike/posts/media` folds into the same media directory. It used to be a second
search path in the web UI; events imported from the Phase 0 corpus reference
those filenames, so they have to come along or their flyers 404.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import paths

SOURCE_ROOT = Path(__file__).parent.parent


def _copy_file(src: Path, dest: Path, report: list[str], label: str) -> None:
    if not src.exists() or dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    report.append(f"{label}: {src} -> {dest}")


def _copy_tree(src: Path, dest: Path, report: list[str], label: str) -> None:
    """Per-file so an existing destination file always wins, and so a partial
    previous run resumes instead of starting over."""
    if not src.is_dir():
        return
    dest.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for item in sorted(src.iterdir()):
        if not item.is_file():
            continue
        target = dest / item.name
        if target.exists():
            skipped += 1
            continue
        shutil.copy2(item, target)
        copied += 1
    if copied or skipped:
        report.append(f"{label}: {copied} copied, {skipped} already present -> {dest}")


def pending() -> bool:
    """Is there in-tree data that has not been migrated yet?

    Drives the hint printed on startup. Keyed on the database specifically:
    it is the file whose absence actually costs the user something.
    """
    return (SOURCE_ROOT / "data" / "calendar.db").exists() and not paths.DB_PATH.exists()


def run(source_root: Path | None = None) -> list[str]:
    """Copy an in-tree install into `paths.HOME`. Returns a line per action."""
    root = Path(source_root) if source_root else SOURCE_ROOT
    paths.ensure()
    report: list[str] = []

    _copy_file(root / "data" / "calendar.db", paths.DB_PATH, report, "database")
    _copy_tree(root / "data" / "media", paths.MEDIA_DIR, report, "media")
    # After the real media, so a name collision resolves in favour of the
    # scraped copy rather than the Phase 0 one.
    _copy_tree(root / "spike" / "posts" / "media", paths.MEDIA_DIR, report, "spike media")
    _copy_tree(root / "data" / "avatars", paths.AVATAR_DIR, report, "avatars")
    _copy_file(root / "config.json", paths.CONFIG_PATH, report, "settings")

    for name, dest in ((".env", paths.ENV_PATH), (".env.local", paths.ENV_LOCAL_PATH)):
        _copy_file(root / name, dest, report, "secrets")
        if dest.exists():
            dest.chmod(0o600)

    return report
