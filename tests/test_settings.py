"""Web settings, including the source-specific automatic refresh defaults."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from local_calendar import config, web


def test_settings_shows_and_saves_all_three_refresh_intervals(monkeypatch):
    state = dict(config.DEFAULTS)

    def save(values):
        state.update(values)
        return dict(state)

    monkeypatch.setattr(web.config, "load", lambda: dict(state))
    monkeypatch.setattr(web.config, "save", save)
    monkeypatch.setattr(web.config, "exists", lambda: True)
    client = web.app.test_client()

    page = client.get("/settings")
    assert page.status_code == 200
    assert b"Automatic refresh" in page.data
    assert b"Instagram accounts (hours)" in page.data
    assert b"Performer webpages (hours)" in page.data
    assert b"Venue webpages (hours)" in page.data

    response = client.post("/settings", data={
        "city": state["city"],
        "radius_miles": state["radius_miles"],
        "timezone": state["timezone"],
        "instagram_refresh_hours": "48",
        "performer_refresh_hours": "12",
        "venue_refresh_hours": "8",
    })
    assert response.status_code == 200
    assert b"Saved." in response.data
    assert state["instagram_refresh_hours"] == 48
    assert state["performer_refresh_hours"] == 12
    assert state["venue_refresh_hours"] == 8


def test_settings_rejects_an_out_of_range_refresh_interval(monkeypatch):
    state = dict(config.DEFAULTS)
    saved = []
    monkeypatch.setattr(web.config, "load", lambda: dict(state))
    monkeypatch.setattr(web.config, "save", lambda values: saved.append(values))
    monkeypatch.setattr(web.config, "exists", lambda: True)

    response = web.app.test_client().post("/settings", data={
        "city": state["city"],
        "radius_miles": state["radius_miles"],
        "timezone": state["timezone"],
        "instagram_refresh_hours": "0",
        "performer_refresh_hours": "6",
        "venue_refresh_hours": "24",
    })
    assert response.status_code == 200
    assert b"Instagram refresh must be between 1 and 720 hours." in response.data
    assert saved == []


def test_local_settings_can_save_a_deepseek_key(monkeypatch):
    writes = []
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(web.config, "write_env",
                        lambda values, replace=False: writes.append((values, replace)))
    monkeypatch.setattr(web.config, "exists", lambda: True)
    client = web.app.test_client()

    page = client.get("/settings")
    assert b'name="deepseek_api_key"' in page.data

    response = client.post("/settings", data={
        "action": "save-deepseek-key",
        "deepseek_api_key": "ds-test-secret",
    })

    assert response.status_code == 200
    assert b"DeepSeek API key saved." in response.data
    assert b"ds-test-secret" not in response.data
    assert writes == [({"DEEPSEEK_API_KEY": "ds-test-secret"}, True)]
    assert os.environ["DEEPSEEK_API_KEY"] == "ds-test-secret"


def test_remote_settings_cannot_see_or_save_a_key(monkeypatch):
    writes = []
    monkeypatch.setattr(web.config, "write_env",
                        lambda values, replace=False: writes.append((values, replace)))
    client = web.app.test_client()

    page = client.get("/settings", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert b'name="deepseek_api_key"' not in page.data

    response = client.post(
        "/settings",
        data={"action": "save-deepseek-key", "deepseek_api_key": "stolen"},
        environ_base={"REMOTE_ADDR": "192.168.1.20"},
    )
    assert response.status_code == 200
    assert b"API keys can only be changed from this Mac." in response.data
    assert writes == []
