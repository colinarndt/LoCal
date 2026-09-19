"""Origin authentication for local installs and Cloudflare Access hosting."""

import pytest

from local_calendar import auth, web


def test_local_mode_needs_no_proxy_headers():
    identity = auth.authenticate({}, {})

    assert identity == auth.Identity(
        subject="local@localhost", email="local@localhost", mode="local"
    )


def test_cloudflare_mode_requires_complete_configuration():
    with pytest.raises(auth.AuthenticationConfigurationError):
        auth.authenticate({}, {auth.AUTH_MODE_ENV: "cloudflare"})


def test_cloudflare_mode_requires_an_assertion():
    env = {
        auth.AUTH_MODE_ENV: "cloudflare",
        auth.TEAM_DOMAIN_ENV: "https://team.cloudflareaccess.com",
        auth.AUDIENCE_ENV: "calendar-audience",
    }

    with pytest.raises(auth.AuthenticationError):
        auth.authenticate({}, env)


def test_verified_cloudflare_claims_become_normalized_identity(monkeypatch):
    monkeypatch.setattr(auth, "_decode_access_token", lambda *args: {
        "sub": "access-user-123", "email": "Friend@Example.COM"
    })
    env = {
        auth.AUTH_MODE_ENV: "cloudflare",
        auth.TEAM_DOMAIN_ENV: "https://team.cloudflareaccess.com",
        auth.AUDIENCE_ENV: "calendar-audience",
    }

    identity = auth.authenticate({auth.ACCESS_HEADER: "signed-token"}, env)

    assert identity == auth.Identity(
        subject="access-user-123", email="friend@example.com", mode="cloudflare"
    )


def test_hosted_web_routes_fail_closed_but_health_check_stays_available(monkeypatch):
    monkeypatch.setenv(auth.AUTH_MODE_ENV, "cloudflare")
    monkeypatch.setenv(auth.TEAM_DOMAIN_ENV, "https://team.cloudflareaccess.com")
    monkeypatch.setenv(auth.AUDIENCE_ENV, "calendar-audience")
    client = web.app.test_client()

    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_private_responses_are_not_stored_by_shared_browser_caches(tmp_path, monkeypatch):
    monkeypatch.setitem(web.app.config, "DB", str(tmp_path / "calendar.db"))

    response = web.app.test_client().get("/")

    assert response.status_code == 200
    assert "private" in response.headers["Cache-Control"]
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["X-Frame-Options"] == "DENY"
