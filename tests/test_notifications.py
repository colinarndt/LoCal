"""Performer alerts reach ntfy on a headless install and stay queued on failure."""

import http.server
import sqlite3
import threading
from dataclasses import replace

import pytest

from local_calendar import notifications, tenancy


class _Capture(http.server.BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def do_POST(self):  # noqa: N802 (http.server naming)
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        type(self).received.append((self.path, dict(self.headers), body))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def ntfy_server():
    _Capture.received = []
    _Capture.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/optiplex", _Capture
    server.shutdown()


def alert(ticket_url="https://tickets.example/show/1"):
    return {
        "title": "Big Thief — Charlotte, Mar 3",
        "body": "New date at The Fillmore, 8 pm",
        "ticket_url": ticket_url,
    }


def queue_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE alert_delivery (id INTEGER PRIMARY KEY, title TEXT, body TEXT, "
        "ticket_url TEXT, delivered_at TEXT)")
    conn.execute(
        "INSERT INTO alert_delivery (title, body, ticket_url) VALUES (?,?,?)",
        ("New show", "Details", None))
    return conn


def test_ntfy_message_carries_title_body_link_and_token(ntfy_server):
    url, capture = ntfy_server
    assert notifications._send_ntfy(alert(), url=url, token="tk_secret")

    path, headers, body = capture.received[0]
    assert path == "/optiplex"
    assert body == "New date at The Fillmore, 8 pm"
    assert headers["Authorization"] == "Bearer tk_secret"
    assert headers["Click"] == "https://tickets.example/show/1"
    assert headers["Actions"] == "view, Get tickets, https://tickets.example/show/1"
    # Non-latin-1 characters in titles degrade instead of breaking delivery.
    assert headers["Title"].startswith("Big Thief")


def test_ntfy_without_ticket_link_sends_no_click_action(ntfy_server):
    url, capture = ntfy_server
    assert notifications._send_ntfy(alert(ticket_url=None), url=url, token="")
    headers = capture.received[0][1]
    assert "Click" not in headers and "Actions" not in headers
    assert "Authorization" not in headers


def test_rejected_or_unreachable_ntfy_keeps_the_alert_queued(ntfy_server, monkeypatch):
    url, capture = ntfy_server
    capture.status = 403
    monkeypatch.setenv(notifications.NTFY_URL_ENV, url)
    conn = queue_db()

    assert notifications.deliver_pending(conn) == 0
    assert conn.execute("SELECT delivered_at FROM alert_delivery").fetchone()[0] is None

    monkeypatch.setenv(notifications.NTFY_URL_ENV, "http://127.0.0.1:9/optiplex")
    assert notifications.deliver_pending(conn) == 0


def test_configured_ntfy_delivers_and_marks_the_alert(ntfy_server, monkeypatch):
    url, capture = ntfy_server
    monkeypatch.setenv(notifications.NTFY_URL_ENV, url)
    monkeypatch.setattr(notifications, "_send_macos",
                        lambda _row: pytest.fail("ntfy is configured"))
    conn = queue_db()

    assert notifications.deliver_pending(conn) == 1
    assert conn.execute("SELECT delivered_at FROM alert_delivery").fetchone()[0]
    assert capture.received[0][2] == "Details"


def test_without_ntfy_the_mac_sender_is_used(monkeypatch):
    monkeypatch.delenv(notifications.NTFY_URL_ENV, raising=False)
    sent = []
    monkeypatch.setattr(notifications, "_send_macos", lambda row: sent.append(row) or True)

    assert notifications._send_default(alert())
    assert len(sent) == 1


def test_hosted_tenants_never_use_the_operators_ntfy_topic(monkeypatch, tmp_path):
    monkeypatch.setenv(notifications.NTFY_URL_ENV, "http://127.0.0.1:9/optiplex")
    monkeypatch.setattr(notifications, "_send_ntfy",
                        lambda _row: pytest.fail("hosted alerts must not go to ntfy"))
    monkeypatch.setattr(notifications, "_send_macos", lambda _row: False)
    hosted = replace(tenancy.local(), id="t1", root=tmp_path, is_local=False)

    with tenancy.activate(hosted):
        assert notifications._send_default(alert()) is False
