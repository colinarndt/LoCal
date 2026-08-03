"""Storing API keys. The failure that matters is silently losing a working one."""

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from social_calendar import config


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_PATH", path)
    return path


def _values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def test_writes_both_keys(env_file):
    config.write_env({"ANTHROPIC_API_KEY": "sk-a", "APIFY_TOKEN": "apify-1"})
    assert _values(env_file) == {"ANTHROPIC_API_KEY": "sk-a", "APIFY_TOKEN": "apify-1"}


def test_secrets_file_is_not_world_readable(env_file):
    config.write_env({"ANTHROPIC_API_KEY": "sk-a"})
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_append_does_not_touch_an_existing_key(env_file):
    """`init` semantics: re-running setup must not clobber a working key."""
    config.write_env({"ANTHROPIC_API_KEY": "sk-original"})
    config.write_env({"ANTHROPIC_API_KEY": "sk-second", "APIFY_TOKEN": "apify-1"})
    got = _values(env_file)
    assert got["ANTHROPIC_API_KEY"] == "sk-original"
    assert got["APIFY_TOKEN"] == "apify-1"


def test_replace_overwrites_and_does_not_duplicate(env_file):
    """The app's key window: the point may be to correct a key that is wrong."""
    config.write_env({"ANTHROPIC_API_KEY": "sk-wrong"})
    config.write_env({"ANTHROPIC_API_KEY": "sk-right"}, replace=True)
    assert env_file.read_text().count("ANTHROPIC_API_KEY=") == 1
    assert _values(env_file)["ANTHROPIC_API_KEY"] == "sk-right"


def test_replacing_one_key_leaves_the_other_alone(env_file):
    """Fixing one key in the app's window must not wipe the one left blank."""
    config.write_env({"ANTHROPIC_API_KEY": "sk-a", "APIFY_TOKEN": "apify-keep"})
    config.write_env({"ANTHROPIC_API_KEY": "sk-new"}, replace=True)
    got = _values(env_file)
    assert got["ANTHROPIC_API_KEY"] == "sk-new"
    assert got["APIFY_TOKEN"] == "apify-keep"


def test_blank_values_are_ignored_not_written_as_empty(env_file):
    """A blank field means "leave it alone", never "set it to nothing"."""
    config.write_env({"ANTHROPIC_API_KEY": "sk-a", "APIFY_TOKEN": ""})
    assert "APIFY_TOKEN" not in _values(env_file)

    config.write_env({"ANTHROPIC_API_KEY": ""}, replace=True)
    assert _values(env_file)["ANTHROPIC_API_KEY"] == "sk-a"


def test_unrelated_lines_survive(env_file):
    """Someone may keep other variables in this file by hand."""
    env_file.write_text("SOMETHING_ELSE=keep-me\nANTHROPIC_API_KEY=sk-old\n")
    config.write_env({"ANTHROPIC_API_KEY": "sk-new"}, replace=True)
    got = _values(env_file)
    assert got["SOMETHING_ELSE"] == "keep-me"
    assert got["ANTHROPIC_API_KEY"] == "sk-new"


def test_creates_the_directory_if_it_is_missing(tmp_path, monkeypatch):
    """First run on a fresh machine: nothing exists yet."""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "nested" / "deeper" / ".env")
    config.write_env({"ANTHROPIC_API_KEY": "sk-a"})
    assert config.ENV_PATH.exists()


# --- the dock setting -------------------------------------------------------

def test_show_in_dock_defaults_off(tmp_path):
    assert config.load(tmp_path / "absent.json")["show_in_dock"] is False


def test_show_in_dock_round_trips(tmp_path):
    path = tmp_path / "config.json"
    config.save({"show_in_dock": True}, path)
    assert config.load(path)["show_in_dock"] is True
    config.save({"show_in_dock": False}, path)
    assert config.load(path)["show_in_dock"] is False


# --- automatic refresh settings --------------------------------------------

def test_refresh_intervals_have_product_defaults(tmp_path):
    cfg = config.load(tmp_path / "absent.json")
    assert cfg["instagram_refresh_hours"] == 24
    assert cfg["performer_refresh_hours"] == 6
    assert cfg["venue_refresh_hours"] == 24


def test_refresh_intervals_round_trip(tmp_path):
    path = tmp_path / "config.json"
    config.save({
        "instagram_refresh_hours": 48,
        "performer_refresh_hours": 12,
        "venue_refresh_hours": 8,
    }, path)
    cfg = config.load(path)
    assert cfg["instagram_refresh_hours"] == 48
    assert cfg["performer_refresh_hours"] == 12
    assert cfg["venue_refresh_hours"] == 8


def test_invalid_stored_refresh_intervals_fall_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"instagram_refresh_hours": 0, "performer_refresh_hours": "nope", '
                    '"venue_refresh_hours": 721}')
    cfg = config.load(path)
    assert cfg["instagram_refresh_hours"] == 24
    assert cfg["performer_refresh_hours"] == 6
    assert cfg["venue_refresh_hours"] == 24
