"""One job slot, shared by the web UI and the menu bar.

The menubar app added a second and a third way to start a paid run: a "Fetch
Now" menu item and a scheduler thread. All of them go through `start_fetch`, so
this pins the contract that stops two concurrent scrapes from billing twice.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from local_calendar import web


@pytest.fixture(autouse=True)
def _idle_job(monkeypatch):
    """Reset the module-level job slot, and satisfy the API-key precondition."""
    web.JOB.update(state="idle", handle=None, label="", message="", stats=None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("APIFY_TOKEN", "test-token")
    # Never let a test reach the network or spend money.
    monkeypatch.setattr(web.threading, "Thread", _FakeThread)
    yield
    web.JOB.update(state="idle", handle=None, label="", message="", stats=None)


class _FakeThread:
    started = 0

    def __init__(self, *a, **kw):
        pass

    def start(self):
        type(self).started += 1


def test_a_second_run_is_refused_while_one_is_running():
    assert web.start_fetch(["a", "b"], "first") is None
    assert web.JOB["state"] == "running"
    # This is the menubar scheduler firing mid-fetch.
    assert web.start_fetch(["a", "b"], "scheduled") == "busy"


def test_missing_keys_refuse_before_the_slot_is_claimed():
    os.environ.pop("OPENAI_API_KEY")
    assert web.start_fetch(["a"], "x") == "no-keys"
    # The slot must be left free, or one misconfigured run wedges the app.
    assert web.JOB["state"] == "idle"


def test_website_only_fetch_needs_no_api_keys():
    os.environ.pop("OPENAI_API_KEY")
    os.environ.pop("APIFY_TOKEN")
    assert web.start_fetch([], "website", [3]) is None
    assert web.JOB["state"] == "running"


def test_a_single_handle_is_recorded_for_the_progress_banner():
    web.start_fetch(["venue"], "@venue")
    assert web.JOB["handle"] == "venue"


def test_a_batch_records_no_single_handle():
    web.start_fetch(["a", "b", "c"], "3 accounts")
    assert web.JOB["handle"] is None
    assert web.JOB["label"] == "3 accounts"


def test_the_slot_frees_up_once_the_job_finishes():
    web.start_fetch(["a"], "first")
    web.JOB.update(state="done")
    assert web.start_fetch(["a"], "second") is None


def test_website_only_failure_is_reported_as_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(web.runner, "fetch_windows", lambda conn, handles: [])
    monkeypatch.setattr(web.runner, "poll", lambda *args, **kwargs: {
        "websites": {
            "sources": 1, "found": 0, "new": 0, "updated": 0, "errors": 1,
            "error_messages": ["Venue: ValueError: unsupported response"],
        },
        "ingested": 0,
        "processed": {"events": 0, "vision_skipped": 0},
    })

    web._fetch_worker([], [1], str(tmp_path / "calendar.db"), 20)

    assert web.JOB["state"] == "error"
    assert web.JOB["message"] == "Venue: ValueError: unsupported response"
