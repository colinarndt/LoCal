"""`refresh-due` gives a headless local install the Mac app's due-driven schedule."""

import argparse
import json

import pytest

from local_calendar import cli, db, hosted, tenancy


def args_for(tmp_path):
    return argparse.Namespace(db=str(tmp_path / "calendar.db"))


def stub_due(monkeypatch, instagram=False, venues=(), performers=()):
    monkeypatch.setattr(hosted.scheduler, "due", lambda *_a: instagram)
    monkeypatch.setattr(
        hosted.scheduler, "due_venue_source_ids", lambda *_a: list(venues))
    monkeypatch.setattr(
        hosted.scheduler, "due_performer_source_ids", lambda *_a: list(performers))


def test_refresh_due_does_nothing_when_nothing_is_due(tmp_path, monkeypatch, capsys):
    with db.session(tmp_path / "calendar.db"):
        pass
    stub_due(monkeypatch)
    monkeypatch.setattr(
        hosted.runner, "poll",
        lambda *_a, **_k: pytest.fail("nothing was due, so nothing should poll"))

    cli.cmd_refresh_due(args_for(tmp_path))

    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "idle"
    assert result["tenant_id"] == "local"


def test_refresh_due_polls_only_due_websites(tmp_path, monkeypatch, capsys):
    with db.session(tmp_path / "calendar.db"):
        pass
    stub_due(monkeypatch, venues=[3], performers=[8])
    monkeypatch.setattr(hosted.runner, "fetch_windows", lambda *_a: [])
    monkeypatch.setattr(hosted.notifications, "deliver_pending", lambda _c: 0)
    calls = []

    def poll(_conn, source, extractor, handles, **kwargs):
        calls.append((handles, kwargs["website_source_ids"]))
        return {}

    monkeypatch.setattr(hosted.runner, "poll", poll)

    cli.cmd_refresh_due(args_for(tmp_path))

    assert calls == [([], [3, 8])]
    assert json.loads(capsys.readouterr().out)["state"] == "done"


def test_refresh_due_reads_the_local_process_keys(tmp_path, monkeypatch):
    with db.session(tmp_path / "calendar.db") as conn:
        conn.execute(
            "INSERT INTO account "
            "(handle,is_polled,seen_count,added_at,status,last_polled_at) "
            "VALUES ('venue',1,0,'now','approved','2026-01-01T00:00:00+00:00')")
    stub_due(monkeypatch, instagram=True)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_env", lambda: None)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_refresh_due(args_for(tmp_path))

    assert exc.value.code == 1


def test_refresh_due_skips_while_the_web_ui_is_refreshing(tmp_path, monkeypatch, capsys):
    with db.session(tmp_path / "calendar.db"):
        pass
    stub_due(monkeypatch, venues=[3])
    import dataclasses
    tenant = dataclasses.replace(tenancy.local(), root=tmp_path)
    lock = tenancy.acquire_refresh_lock(tenant)
    try:
        cli.cmd_refresh_due(args_for(tmp_path))
    finally:
        tenancy.release_refresh_lock(lock)

    assert json.loads(capsys.readouterr().out)["state"] == "busy"
