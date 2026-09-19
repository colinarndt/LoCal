"""Hosted identities receive stable, physically isolated data roots."""

import sqlite3

from local_calendar import auth, config, db, tenancy, web


def hosted_env(tmp_path):
    return {
        tenancy.HOSTED_ROOT_ENV: str(tmp_path / "hosted"),
        tenancy.OWNER_EMAIL_ENV: "owner@example.com",
    }


def identity(subject, email):
    return auth.Identity(subject=subject, email=email, mode="cloudflare")


def test_resolve_provisions_stable_separate_private_roots(tmp_path):
    env = hosted_env(tmp_path)

    owner = tenancy.resolve(identity("owner-sub", "owner@example.com"), env)
    owner_again = tenancy.resolve(identity("owner-sub", "owner@example.com"), env)
    friend = tenancy.resolve(identity("friend-sub", "friend@example.com"), env)

    assert owner.id == owner_again.id
    assert owner.root == owner_again.root
    assert owner.id != friend.id
    assert owner.role == "owner"
    assert friend.role == "member"
    assert owner.media_dir.is_dir()
    assert friend.avatar_dir.is_dir()
    assert owner.root.stat().st_mode & 0o777 == 0o700

    registry = sqlite3.connect(tmp_path / "hosted" / "registry.db")
    assert registry.execute("SELECT COUNT(*) FROM app_user").fetchone()[0] == 2
    registry.close()


def test_tenant_context_isolates_configuration_files(tmp_path):
    env = hosted_env(tmp_path)
    owner = tenancy.resolve(identity("owner-sub", "owner@example.com"), env)
    friend = tenancy.resolve(identity("friend-sub", "friend@example.com"), env)

    with tenancy.activate(owner):
        config.save({"city": "Charlotte, NC"})
    with tenancy.activate(friend):
        config.save({"city": "Richmond, VA"})

    with tenancy.activate(owner):
        assert config.load()["city"] == "Charlotte, NC"
    with tenancy.activate(friend):
        assert config.load()["city"] == "Richmond, VA"


def test_hosted_deepseek_keys_and_job_slots_are_per_tenant(tmp_path, monkeypatch):
    env = hosted_env(tmp_path)
    owner = tenancy.resolve(identity("owner-sub", "owner@example.com"), env)
    friend = tenancy.resolve(identity("friend-sub", "friend@example.com"), env)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak-from-process")
    monkeypatch.setenv("APIFY_TOKEN", "shared-apify-token")
    monkeypatch.setattr(web.threading, "Thread", lambda **_kwargs: type(
        "Thread", (), {"start": lambda self: None}
    )())

    with tenancy.activate(owner):
        assert config.secret("DEEPSEEK_API_KEY") is None
        config.write_env({"DEEPSEEK_API_KEY": "owner-key"})
        assert config.secret("DEEPSEEK_API_KEY") == "owner-key"
        assert config.secret("APIFY_TOKEN") == "shared-apify-token"
        assert web.start_fetch(["owner-source"], "owner refresh") is None
        assert web._job_for()["state"] == "running"

    with tenancy.activate(friend):
        assert config.secret("DEEPSEEK_API_KEY") is None
        config.write_env({"DEEPSEEK_API_KEY": "friend-key"})
        assert config.secret("DEEPSEEK_API_KEY") == "friend-key"
        assert web._job_for()["state"] == "idle"
        assert web.start_fetch(["friend-source"], "friend refresh") is None

    with tenancy.activate(owner):
        assert web._job_for()["label"] == "owner refresh"
    with tenancy.activate(friend):
        assert web._job_for()["label"] == "friend refresh"


def test_hosted_web_requests_use_separate_databases(tmp_path, monkeypatch):
    claims = {
        "owner-token": {"sub": "owner-sub", "email": "owner@example.com"},
        "friend-token": {"sub": "friend-sub", "email": "friend@example.com"},
    }
    monkeypatch.setattr(
        auth, "_decode_access_token", lambda token, *_args: claims[token]
    )
    monkeypatch.setenv(auth.AUTH_MODE_ENV, "cloudflare")
    monkeypatch.setenv(auth.TEAM_DOMAIN_ENV, "https://team.cloudflareaccess.com")
    monkeypatch.setenv(auth.AUDIENCE_ENV, "calendar-audience")
    monkeypatch.setenv(tenancy.HOSTED_ROOT_ENV, str(tmp_path / "hosted"))
    monkeypatch.setenv(tenancy.OWNER_EMAIL_ENV, "owner@example.com")
    client = web.app.test_client()

    owner_headers = {auth.ACCESS_HEADER: "owner-token"}
    friend_headers = {auth.ACCESS_HEADER: "friend-token"}
    assert client.get("/", headers=owner_headers).status_code == 200
    assert client.get("/", headers=friend_headers).status_code == 200

    owner = tenancy.resolve(identity("owner-sub", "owner@example.com"))
    friend = tenancy.resolve(identity("friend-sub", "friend@example.com"))
    assert owner.db_path.exists()
    assert friend.db_path.exists()
    assert owner.db_path != friend.db_path

    with db.session(owner.db_path) as conn:
        conn.execute(
            "INSERT INTO source_post "
            "(post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('owner-post','venue','2099-08-01','now')"
        )
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,created_at) "
            "VALUES ('owner-post','Owner-only concert','2099-08-01','now')"
        )

    assert b"Owner-only concert" in client.get("/", headers=owner_headers).data
    assert b"Owner-only concert" not in client.get("/", headers=friend_headers).data


def test_only_owner_can_assign_user_keys_and_cross_site_posts_are_rejected(
        tmp_path, monkeypatch):
    claims = {
        "owner-token": {"sub": "owner-sub", "email": "owner@example.com"},
        "friend-token": {"sub": "friend-sub", "email": "friend@example.com"},
    }
    monkeypatch.setattr(
        auth, "_decode_access_token", lambda token, *_args: claims[token]
    )
    monkeypatch.setenv(auth.AUTH_MODE_ENV, "cloudflare")
    monkeypatch.setenv(auth.TEAM_DOMAIN_ENV, "https://team.cloudflareaccess.com")
    monkeypatch.setenv(auth.AUDIENCE_ENV, "calendar-audience")
    monkeypatch.setenv(tenancy.HOSTED_ROOT_ENV, str(tmp_path / "hosted"))
    monkeypatch.setenv(tenancy.OWNER_EMAIL_ENV, "owner@example.com")
    client = web.app.test_client()
    owner_headers = {auth.ACCESS_HEADER: "owner-token"}
    friend_headers = {auth.ACCESS_HEADER: "friend-token"}

    # First login provisions both users in the central registry.
    assert client.get("/", headers=owner_headers).status_code == 200
    assert client.get("/", headers=friend_headers).status_code == 200
    assert client.get("/admin/users", headers=friend_headers).status_code == 403

    page = client.get("/admin/users", headers=owner_headers)
    assert page.status_code == 200
    assert b"owner@example.com" in page.data
    assert b"friend@example.com" in page.data
    friend = tenancy.resolve(identity("friend-sub", "friend@example.com"))
    path = f"/admin/users/{friend.id}/deepseek-key"

    # An authenticated Access cookie alone is insufficient for a hosted write.
    rejected = client.post(
        path, headers=owner_headers, data={"deepseek_api_key": "friend-secret"}
    )
    assert rejected.status_code == 403

    saved = client.post(
        path,
        headers={**owner_headers, "Origin": "http://localhost"},
        data={"deepseek_api_key": "friend-secret"},
    )
    assert saved.status_code == 302
    with tenancy.activate(friend):
        assert config.secret("DEEPSEEK_API_KEY") == "friend-secret"

    page = client.get("/admin/users", headers=owner_headers)
    assert b"DeepSeek key: set" in page.data
    assert b"friend-secret" not in page.data


def test_hosted_root_must_be_absolute(tmp_path):
    env = {
        tenancy.HOSTED_ROOT_ENV: "relative/data",
        tenancy.OWNER_EMAIL_ENV: "owner@example.com",
    }

    try:
        tenancy.resolve(identity("owner-sub", "owner@example.com"), env)
    except tenancy.TenancyConfigurationError as exc:
        assert "absolute path" in str(exc)
    else:
        raise AssertionError("relative hosted root was accepted")
