"""Installable web-app shell and hosting liveness checks."""

import json

from local_calendar import web


def test_manifest_describes_a_root_scoped_standalone_app():
    response = web.app.test_client().get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json"
    manifest = json.loads(response.data)
    assert manifest["name"] == "LoCal"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}


def test_service_worker_can_control_the_whole_application():
    response = web.app.test_client().get("/service-worker.js")

    assert response.status_code == 200
    assert response.mimetype == "application/javascript"
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert "no-cache" in response.headers["Cache-Control"]
    assert b'event.request.mode === "navigate"' in response.data
    assert b'/media/' not in response.data


def test_pages_advertise_the_manifest_and_worker(tmp_path, monkeypatch):
    monkeypatch.setitem(web.app.config, "DB", str(tmp_path / "calendar.db"))
    client = web.app.test_client()

    for url in ("/", "/settings", "/discover", "/trips"):
        response = client.get(url)
        assert response.status_code == 200
        assert b'rel="manifest" href="/manifest.webmanifest"' in response.data
        assert b'src="/static/pwa.js"' in response.data


def test_health_check_does_not_need_a_database():
    response = web.app.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
