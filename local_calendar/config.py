"""Per-install settings: which city, how far out, which timezone.

Deliberately split from the API keys. Secrets stay in `.env` (read only through
`os.getenv`, never written by this app); everything here is non-secret and lives
in `config.json`, so the web settings page can rewrite it without ever holding a
file that contains credentials. Both live in the app's data directory -- see
`paths.py` for why that is not the source tree.

Written by `cli init`, edited by `/settings`, read by everything else.
"""

from __future__ import annotations

import datetime as dt
import json
import zoneinfo
from pathlib import Path

from .paths import CONFIG_PATH, ENV_PATH  # noqa: F401  (re-exported; callers import from here)

# Asked for by `cli init`, reported (present/absent, never the value) by /settings.
API_KEYS = [
    ("OPENAI_API_KEY", "OpenAI API key",
     "https://platform.openai.com/api-keys"),
    ("APIFY_TOKEN", "Apify API token",
     "https://console.apify.com/settings/integrations"),
]

REFRESH_HOURS_MIN = 1
REFRESH_HOURS_MAX = 720
REFRESH_INTERVAL_KEYS = (
    "instagram_refresh_hours",
    "performer_refresh_hours",
    "venue_refresh_hours",
)

# Charlotte, NC -- the author's city, and a working example rather than a
# baked-in assumption. `cli init` overwrites all of it.
DEFAULTS = {
    "city": "Charlotte, NC",
    "lat": 35.2271,
    "lon": -80.8431,
    "radius_miles": 25.0,
    # Nominatim's postcode lookup needs a country to disambiguate; `geocode_zip`
    # reads it. Its absence here made the calendar's zip filter raise KeyError.
    "country": "us",
    "timezone": "America/New_York",
    # Automatic refresh cadence by source type. These stay separate so a cheap
    # website check cannot move the clock for a paid Instagram poll, and one
    # recently checked website cannot postpone another.
    "instagram_refresh_hours": 24,
    "performer_refresh_hours": 6,
    "venue_refresh_hours": 24,
    # Mac app only. Off by default: it is a menu bar app, and a Dock tile for
    # something that mostly runs a nightly job is clutter. On means a Dock icon
    # and a Cmd-Tab entry -- see `app.apply_dock_policy`.
    "show_in_dock": False,
}


def write_env(values: dict[str, str], replace: bool = False) -> None:
    """Store API keys, mode 0600. The only file this app writes that holds secrets.

    `replace=False` (what `init` does) appends only keys that are not already
    present, so re-running setup cannot clobber a working key with a blank field.
    `replace=True` is for the app's key window, where the whole point may be to
    correct a key that is present but wrong.
    """
    existing = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    kept = []
    for line in existing.splitlines():
        name = line.split("=", 1)[0].strip()
        if replace and name in values and values[name]:
            continue        # about to be re-added with the new value
        kept.append(line)

    present = {ln.split("=", 1)[0].strip() for ln in kept}
    for key, value in values.items():
        if value and key not in present:
            kept.append(f"{key}={value}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(ln for ln in kept if ln.strip()) + "\n")
    ENV_PATH.chmod(0o600)


def system_timezone() -> str:
    """Best guess at an IANA zone name for the machine we are running on.

    `datetime.now().astimezone().tzname()` gives an abbreviation ("EDT"), which
    is *not* a valid ICS TZID -- so prefer /etc/localtime's symlink target and
    only accept a name zoneinfo actually knows.
    """
    link = Path("/etc/localtime")
    if link.is_symlink():
        name = "/".join(link.resolve().parts[-2:])
        if is_valid_timezone(name):
            return name
    return DEFAULTS["timezone"]


def is_valid_timezone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except Exception:
        return False
    return name in zoneinfo.available_timezones()


def load(path: Path | str = CONFIG_PATH) -> dict:
    """Settings with defaults filled in. Missing file is not an error -- the app
    stays usable before `init` runs, just pointed at the example city."""
    cfg = dict(DEFAULTS)
    try:
        with open(path) as fh:
            stored = json.load(fh)
            if isinstance(stored, dict):
                # Discard retired settings (including the former home ZIP).
                # Keeping them around would make a saved config misleading.
                cfg.update({key: value for key, value in stored.items() if key in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not is_valid_timezone(cfg["timezone"]):
        cfg["timezone"] = DEFAULTS["timezone"]
    for key in REFRESH_INTERVAL_KEYS:
        try:
            hours = int(cfg[key])
            if not REFRESH_HOURS_MIN <= hours <= REFRESH_HOURS_MAX:
                raise ValueError
            cfg[key] = hours
        except (TypeError, ValueError):
            cfg[key] = DEFAULTS[key]
    return cfg


def save(cfg: dict, path: Path | str = CONFIG_PATH) -> dict:
    """Merge over what is already stored and write. Returns the merged result."""
    merged = load(path)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    with open(path, "w") as fh:
        json.dump(merged, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return merged


def exists(path: Path | str = CONFIG_PATH) -> bool:
    """Has `init` run? Drives the setup banner in the web UI."""
    return Path(path).exists()


def tzinfo(cfg: dict | None = None) -> dt.tzinfo:
    return zoneinfo.ZoneInfo((cfg or load())["timezone"])
