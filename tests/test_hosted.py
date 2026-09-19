"""Unattended hosted refreshes stay due-driven, isolated, and single-flight."""

from local_calendar import auth, db, hosted, tenancy


def tenant_at(tmp_path):
    env = {
        tenancy.HOSTED_ROOT_ENV: str(tmp_path / "hosted"),
        tenancy.OWNER_EMAIL_ENV: "owner@example.com",
    }
    return tenancy.resolve(
        auth.Identity("owner-sub", "owner@example.com", "cloudflare"), env
    )


def empty_stats():
    return {
        "websites": {"sources": 1, "new": 0, "errors": 0},
        "ingested": 0,
        "processed": {"events": 0, "vision_skipped": 0},
    }


def test_hosted_refresh_checks_only_due_sources(tmp_path, monkeypatch):
    tenant = tenant_at(tmp_path)
    with db.session(tenant.db_path):
        pass
    monkeypatch.setattr(hosted.scheduler, "due", lambda *_args: False)
    monkeypatch.setattr(
        hosted.scheduler, "due_venue_source_ids", lambda *_args: [17]
    )
    monkeypatch.setattr(
        hosted.scheduler, "due_performer_source_ids", lambda *_args: []
    )
    monkeypatch.setattr(hosted.runner, "fetch_windows", lambda *_args: [])
    calls = []

    def poll(_conn, source, extractor, handles, **kwargs):
        calls.append((source, extractor, handles, kwargs["website_source_ids"]))
        return empty_stats()

    monkeypatch.setattr(hosted.runner, "poll", poll)
    monkeypatch.setattr(hosted.notifications, "deliver_pending", lambda _conn: 0)

    result = hosted.refresh_tenant(tenant)

    assert result["state"] == "done"
    assert calls == [(None, None, [], [17])]


def test_hosted_instagram_refresh_requires_tenant_deepseek_key(
        tmp_path, monkeypatch):
    tenant = tenant_at(tmp_path)
    with db.session(tenant.db_path) as conn:
        conn.execute(
            "INSERT INTO account "
            "(handle,is_polled,seen_count,added_at,status,last_polled_at) "
            "VALUES ('venue',1,0,'now','approved','2026-01-01T00:00:00+00:00')"
        )
    monkeypatch.setenv("APIFY_TOKEN", "shared-apify")
    monkeypatch.setattr(hosted.scheduler, "due", lambda *_args: True)
    monkeypatch.setattr(
        hosted.scheduler, "due_venue_source_ids", lambda *_args: []
    )
    monkeypatch.setattr(
        hosted.scheduler, "due_performer_source_ids", lambda *_args: []
    )

    result = hosted.refresh_tenant(tenant)

    assert result["state"] == "error"
    assert result["message"] == "DeepSeek key is missing"


def test_scheduler_does_not_overlap_a_web_refresh(tmp_path):
    tenant = tenant_at(tmp_path)
    with db.session(tenant.db_path):
        pass
    lock = tenancy.acquire_refresh_lock(tenant)
    try:
        result = hosted.refresh_tenant(tenant)
    finally:
        tenancy.release_refresh_lock(lock)

    assert result["state"] == "busy"
    assert result["message"] == "refresh already running"
