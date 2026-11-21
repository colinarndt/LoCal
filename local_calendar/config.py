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
import os
import zoneinfo
from pathlib import Path

from dotenv import dotenv_values

from . import tenancy
from .paths import CONFIG_PATH, ENV_PATH  # noqa: F401  (re-exported; callers import from here)

# Asked for by `cli init`, reported (present/absent, never the value) by /settings.
API_KEYS = [
    ("DEEPSEEK_API_KEY", "DeepSeek API key",
     "https://platform.deepseek.com/api_keys"),
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


def _current_env_path() -> Path:
    tenant = tenancy.current()
    return Path(ENV_PATH) if tenant.is_local else tenant.env_path


def _current_config_path() -> Path:
    tenant = tenancy.current()
    return Path(CONFIG_PATH) if tenant.is_local else tenant.config_path


def secret(name: str) -> str | None:
    """Return a provider secret without crossing hosted tenant boundaries.

    Local mode keeps its historical process environment. Hosted DeepSeek keys
    must exist in the current tenant's mode-600 file; falling back to the
    owner's process key would silently charge the wrong person. Apify may be a
    shared server account, while its spend is still recorded per tenant.
    """
    tenant = tenancy.current()
    if tenant.is_local:
        return os.getenv(name)

    values = {}
    for path in (tenant.env_local_path, tenant.env_path):
        if path.exists():
            # First file wins, matching the local load_dotenv order.
            for key, value in dotenv_values(path).items():
                values.setdefault(key, value)
    value = str(values.get(name) or "").strip()
    if value:
        return value
    if name == "APIFY_TOKEN":
        return os.getenv(name)
    return None


def write_env(values: dict[str, str], replace: bool = False,
              path: Path | str | None = None) -> None:
    """Store API keys, mode 0600. The only file this app writes that holds secrets.

    `replace=False` (what `init` does) appends only keys that are not already
    present, so re-running setup cannot clobber a working key with a blank field.
    `replace=True` is for the app's key window, where the whole point may be to
    correct a key that is present but wrong.
    """
    env_path = Path(path) if path is not None else _current_env_path()
    existing = env_path.read_text() if env_path.exists() else ""
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

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(ln for ln in kept if ln.strip()) + "\n")
    env_path.chmod(0o600)


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


def load(path: Path | str | None = None) -> dict:
    """Settings with defaults filled in. Missing file is not an error -- the app
    stays usable before `init` runs, just pointed at the example city."""
    path = Path(path) if path is not None else _current_config_path()
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


def save(cfg: dict, path: Path | str | None = None) -> dict:
    """Merge over what is already stored and write. Returns the merged result."""
    path = Path(path) if path is not None else _current_config_path()
    merged = load(path)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    with open(path, "w") as fh:
        json.dump(merged, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return merged


def exists(path: Path | str | None = None) -> bool:
    """Has `init` run? Drives the setup banner in the web UI."""
    path = Path(path) if path is not None else _current_config_path()
    return path.exists()


def tzinfo(cfg: dict | None = None) -> dt.tzinfo:
    return zoneinfo.ZoneInfo((cfg or load())["timezone"])
