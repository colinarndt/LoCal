"""Web settings, including the source-specific automatic refresh defaults."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from social_calendar import config, web


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
