"""Where this install keeps its data.

Everything writable -- the database, scraped flyers, avatars, settings, and the
API keys -- resolves through here rather than off `__file__`. Two reasons:

  * `pip install -e .` and a py2app bundle both put the package somewhere you
    must not write to. Inside a bundle, `Path(__file__).parent.parent` lands in
    `Contents/`, which breaks code signing and is wiped on every update.
  * `data/media` is the biggest thing this app owns (200MB+ and growing). It
    belongs beside the database, not inside the application.

`SOCIAL_CALENDAR_HOME` overrides the lot. Tests point it at a tmpdir; a dev who
wants the old repo-tree layout back can export it and get exactly that.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "instagram-calendar"


def _default_home() -> Path:
    """Per-platform convention for "an app's own private data".

    Not `~/.instagram-calendar`: on macOS a dotdir in $HOME is invisible in
    Finder, which matters here because the user is expected to go look at
    (and back up) their own calendar.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        base = os.getenv("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / APP_NAME
    base = os.getenv("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_NAME


def home() -> Path:
    """Resolved once per call so tests can move it between cases."""
    override = os.getenv("SOCIAL_CALENDAR_HOME")
    return Path(override).expanduser() if override else _default_home()


# Module-level constants, because every caller already imports them as such and
# a process does not change its data directory mid-run. `home()` stays available
# for the tests and the migration, which do.
HOME = home()

DB_PATH = HOME / "calendar.db"
MEDIA_DIR = HOME / "media"
AVATAR_DIR = HOME / "avatars"
CONFIG_PATH = HOME / "config.json"

# Secrets stay in their own file, separate from config.json, so the web settings
# page can rewrite settings without ever holding a file that contains keys.
ENV_PATH = HOME / ".env"
ENV_LOCAL_PATH = HOME / ".env.local"


def ensure() -> Path:
    """Create the directory tree. Safe to call repeatedly; called on startup by
    both entry points so a fresh install works before `init` has ever run."""
    for d in (HOME, MEDIA_DIR, AVATAR_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return HOME
