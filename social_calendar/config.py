"""Per-install settings: which city, how far out, which timezone.

Deliberately split from the API keys. Secrets stay in `.env` (read only through
`os.getenv`, never written by this app); everything here is non-secret and lives
in `config.json`, so the web settings page can rewrite it without ever holding a
file that contains credentials.

Written by `cli init`, edited by `/settings`, read by everything else.
"""

from __future__ import annotations

import datetime as dt
import json
import zoneinfo
from pathlib import Path

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"

# Asked for by `cli init`, reported (present/absent, never the value) by /settings.
API_KEYS = [
    ("ANTHROPIC_API_KEY", "Anthropic API key",
     "https://platform.claude.com/settings/keys"),
    ("APIFY_TOKEN", "Apify API token",
     "https://console.apify.com/settings/integrations"),
]

# Charlotte, NC -- the author's city, and a working example rather than a
# baked-in assumption. `cli init` overwrites all of it.
DEFAULTS = {
    "city": "Charlotte, NC",
    "lat": 35.2271,
    "lon": -80.8431,
    "radius_miles": 25.0,
    "timezone": "America/New_York",
    "country": "USA",
}


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
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    if not is_valid_timezone(cfg["timezone"]):
        cfg["timezone"] = DEFAULTS["timezone"]
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
