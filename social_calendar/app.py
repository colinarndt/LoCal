"""The menu bar app: status item, embedded calendar, self-scheduling daily run.

    python -m social_calendar.app

Written straight against AppKit rather than on rumps + pywebview. Both of those
want to own the main thread's NSApplication run loop, and there is exactly one;
combining them is a fight, and freezing pywebview into a bundle adds a
hidden-import tail on top. A status item and a WKWebView in a window is what
those libraries wrap anyway, and doing it directly costs two dependencies
instead of four.

Three things share this process:

  * Flask, on a daemon thread, serving the same UI as `python -m social_calendar.web`
  * a WKWebView pointed at it, so the calendar opens *in* the app -- no browser
  * a scheduler thread that runs the daily poll when one is overdue

The scheduler and the "Fetch Now" item both go through `web.start_fetch`, so
they contend for the single job slot the web UI already owns rather than
starting a second concurrent scrape.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                    NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
                    NSButton, NSFont, NSImage, NSMenu, NSMenuItem, NSObject,
                    NSScreen, NSSecureTextField, NSTextField,
                    NSVariableStatusItemLength, NSStatusBar, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSTimer, NSURL, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration

from . import config, db, discovery, paths, scheduler, spend, web

PREFERRED_PORT = 8730
WINDOW_SIZE = (1180, 860)
SETTINGS_POLL_SECONDS = 30.0
KEYS_WINDOW_SIZE = (560, 330)


def missing_keys() -> list[tuple[str, str, str]]:
    """Which API keys are absent from the environment right now."""
    return [(name, label, url) for name, label, url in config.API_KEYS
            if not os.getenv(name)]


def apply_dock_policy(show_in_dock: bool) -> None:
    """Show or hide the Dock tile and the Cmd-Tab entry.

    `LSUIElement` in Info.plist decides what the app launches as; this overrides
    it at runtime, so the setting takes effect immediately instead of on the next
    launch. Accessory is the default because a menu bar app that mostly runs a
    nightly job does not need a Dock tile -- but it also means Cmd-Tab cannot
    reach it, which is worth being able to turn off.
    """
    app = NSApplication.sharedApplication()
    wanted = (NSApplicationActivationPolicyRegular if show_in_dock
              else NSApplicationActivationPolicyAccessory)
    if app.activationPolicy() != wanted:
        app.setActivationPolicy_(wanted)


def _pick_port(preferred: int = PREFERRED_PORT) -> int:
    """Keep 8730 when it is free.

    Not cosmetic: `/calendar.ics` is subscribed to by URL on a phone. A random
    port every launch silently breaks that subscription, so only move if the
    port is genuinely taken.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _serve(port: int) -> None:
    """Flask on a daemon thread.

    Bound to 0.0.0.0, not localhost: the point of this app is also to have the
    calendar on a phone over Tailscale/LAN, which the embedded webview does not
    replace. `use_reloader` off is not optional -- the reloader forks, and a
    forked NSApplication does not survive it.
    """
    web.app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False,
                threaded=True)


def _money(usd: float) -> str:
    """Sub-cent totals are the normal case early on, and "$0.00" next to a
    growing call count reads like the tracking is broken."""
    if usd and usd < 0.01:
        return "<$0.01"
    return f"${usd:,.2f}"


class AppDelegate(NSObject):

    # PyObjC constructs via alloc().init(); __init__ is not called for us.
    def initWithPort_(self, port):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self.window = None
        self.webview = None
        self.keys_window = None
        self.key_fields = {}
        self._stop = threading.Event()
        return self

    # --- lifecycle ----------------------------------------------------------

    def applicationDidFinishLaunching_(self, notification):
        bar = NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        button = self.status_item.button()
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "calendar", "Instagram Calendar")
        if icon is not None:
            icon.setTemplate_(True)      # tints itself for light/dark menu bars
            button.setImage_(icon)

        self.menu = NSMenu.alloc().init()
        self.menu.setDelegate_(self)     # menuWillOpen_ refreshes the numbers
        self.status_item.setMenu_(self.menu)
        self._rebuild_menu()
        self.settings_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                SETTINGS_POLL_SECONDS, self, "pollSettings:", None, True))

        apply_dock_policy(config.load().get("show_in_dock", False))

        threading.Thread(target=self._schedule_loop, daemon=True).start()

        # Show the calendar on launch. This is a double-clickable app, and one
        # that starts by putting a small icon in a crowded menu bar and nothing
        # else looks like it failed to start. Closing the window leaves the app
        # running in the menu bar, which is the behaviour that actually matters.
        self.openCalendar_(None)

        # First run: nothing works without keys, and the calendar behind this
        # window will be empty, so ask straight away rather than letting someone
        # discover it via a failed fetch.
        if missing_keys():
            self.showKeys_(None)

    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        # Closing the calendar window hides it; the app lives in the menu bar.
        return False

    # --- menu ---------------------------------------------------------------

    @objc.python_method
    def _item(self, title, action=None, key=""):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key)
        if action:
            item.setTarget_(self)
        else:
            item.setEnabled_(False)      # informational rows
        return item

    @objc.python_method
    def _rebuild_menu(self):
        self.menu.removeAllItems()
        self.menu.addItem_(self._item("Open Calendar", "openCalendar:", "o"))
        self.menu.addItem_(self._item(self._fetch_title(), "fetchNow:", "f"))
        self.menu.addItem_(NSMenuItem.separatorItem())

        for line in self._status_lines():
            self.menu.addItem_(self._item(line))

        self.menu.addItem_(NSMenuItem.separatorItem())

        dock = self._item("Show in Dock", "toggleDock:")
        dock.setState_(1 if config.load().get("show_in_dock", False) else 0)
        self.menu.addItem_(dock)
        self.menu.addItem_(self._item("API Keys…", "showKeys:"))

        self.menu.addItem_(NSMenuItem.separatorItem())
        self.menu.addItem_(self._item("Quit", "quit:", "q"))

    def menuWillOpen_(self, menu):
        """Numbers are read when the menu opens, not polled on a timer."""
        self._rebuild_menu()

    @objc.python_method
    def _fetch_title(self) -> str:
        return "Fetching…" if web.JOB["state"] == "running" else "Fetch Now"

    @objc.python_method
    def _status_lines(self) -> list[str]:
        try:
            # read_session, not session: this runs on the main thread when the
            # menu opens, and `connect` would block behind a running fetch.
            with db.read_session(web.app.config["DB"]) as conn:
                totals = spend.totals(conn)
                last = scheduler.describe(conn)
                polled = len(discovery.approved_handles(conn))
        except Exception as exc:
            return [f"database unavailable: {type(exc).__name__}"]

        lines = [f"Last run: {last}", f"Watching {polled} accounts"]

        absent = missing_keys()
        if absent:
            # Named rather than just counted: "set your API keys" sends someone
            # to check both when only one is actually missing.
            lines.append(f"⚠ no {' or '.join(name for name, _, _ in absent)}")

        if web.JOB["state"] == "running" and web.JOB.get("message"):
            lines.append(f"  {web.JOB['message']}")

        lines.append("")
        if not totals["since"]:
            lines.append("No spend recorded yet")
            return lines

        lines.append(f"Spend, last 24h: {_money(totals['last_24h'])}")
        # Never labelled "all time": this ledger postdates the install, so spend
        # before it started cannot be reconstructed. Say when counting began
        # instead of showing a number that quietly understates the real total.
        lines.append(f"Since {totals['since'][:10]}: {_money(totals['all_time'])} "
                     f"({totals['calls']} calls)")
        if totals.get("estimated_usd"):
            # Apify reports actual dollars per run, but not for every actor
            # pricing model. Where it did not, say so rather than blending a
            # guess into a figure the user will read as measured.
            lines.append(f"  includes {_money(totals['estimated_usd'])} estimated")
        return lines

    def pollSettings_(self, timer):
        # config.json is the single source of truth for this, so a change made on
        # the web /settings page lands here too rather than only in the menu.
        apply_dock_policy(config.load().get("show_in_dock", False))

    # --- actions ------------------------------------------------------------

    def openCalendar_(self, sender):
        if self.window is None:
            self._build_window()
        else:
            self.webview.reload_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def _build_window(self):
        width, height = WINDOW_SIZE
        screen = NSScreen.mainScreen().visibleFrame()
        rect = NSMakeRect(screen.origin.x + (screen.size.width - width) / 2,
                          screen.origin.y + (screen.size.height - height) / 2,
                          width, height)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        self.window.setTitle_("Calendar")
        # Without this the window is deallocated on close and reopening crashes.
        self.window.setReleasedWhenClosed_(False)

        config = WKWebViewConfiguration.alloc().init()
        self.webview = WKWebView.alloc().initWithFrame_configuration_(
            self.window.contentView().bounds(), config)
        self.webview.setAutoresizingMask_(1 << 1 | 1 << 4)   # width | height
        self.webview.loadRequest_(
            NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))
        self.window.contentView().addSubview_(self.webview)

    def fetchNow_(self, sender):
        self._start_run("menu bar")

    # --- API keys -----------------------------------------------------------
    #
    # A native window rather than a page in the web UI, and not by accident.
    # /settings deliberately refuses to accept key values because Flask binds
    # 0.0.0.0 with no login so a phone can reach it -- a key field there would be
    # a key field for everyone on the network. This window is not reachable over
    # the network at all, so it respects that rule instead of reversing it.

    def showKeys_(self, sender):
        if self.keys_window is None:
            self._build_keys_window()
        self._load_key_fields()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.keys_window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def _label(self, text, frame, *, bold=False, small=False, muted=False):
        field = NSTextField.alloc().initWithFrame_(frame)
        field.setStringValue_(text)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(True)
        size = 11 if small else 13
        field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                       else NSFont.systemFontOfSize_(size))
        if muted:
            from AppKit import NSColor
            field.setTextColor_(NSColor.secondaryLabelColor())
        return field

    @objc.python_method
    def _build_keys_window(self):
        width, height = KEYS_WINDOW_SIZE
        pad = 24
        inner = width - 2 * pad

        screen = NSScreen.mainScreen().visibleFrame()
        rect = NSMakeRect(screen.origin.x + (screen.size.width - width) / 2,
                          screen.origin.y + (screen.size.height - height) / 2,
                          width, height)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        win.setTitle_("API Keys")
        win.setReleasedWhenClosed_(False)
        content = win.contentView()

        y = height - pad - 34
        content.addSubview_(self._label(
            "Both keys are needed before the app can fetch anything. "
            "They are stored on this Mac only.",
            NSMakeRect(pad, y, inner, 34), muted=True))

        self.key_fields = {}
        for name, label, url in config.API_KEYS:
            y -= 30
            content.addSubview_(self._label(label, NSMakeRect(pad, y, inner, 17),
                                            bold=True))
            y -= 17
            content.addSubview_(self._label(f"{name} · {url}",
                                            NSMakeRect(pad, y, inner, 15),
                                            small=True, muted=True))
            y -= 28
            field = NSSecureTextField.alloc().initWithFrame_(
                NSMakeRect(pad, y, inner, 24))
            field.setPlaceholderString_(name)
            content.addSubview_(field)
            self.key_fields[name] = field

        y -= 40
        content.addSubview_(self._label(
            f"Written to {paths.ENV_PATH}, readable only by you (mode 600).",
            NSMakeRect(pad, y, inner, 15), small=True, muted=True))

        save = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - pad - 100, pad, 100, 32))
        save.setTitle_("Save")
        save.setBezelStyle_(1)          # NSBezelStyleRounded
        save.setKeyEquivalent_("\r")    # Return activates it
        save.setTarget_(self)
        save.setAction_("saveKeys:")
        content.addSubview_(save)

        cancel = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - pad - 210, pad, 100, 32))
        cancel.setTitle_("Cancel")
        cancel.setBezelStyle_(1)
        cancel.setKeyEquivalent_("\x1b")   # Escape
        cancel.setTarget_(self)
        cancel.setAction_("closeKeys:")
        content.addSubview_(cancel)

        self.keys_window = win

    @objc.python_method
    def _load_key_fields(self):
        """Show a placeholder for keys already set, never the value itself.

        Leaving a set key's field blank and skipping blanks on save is what makes
        "open the window, fix one key, save" not wipe the other one.
        """
        for name, field in self.key_fields.items():
            field.setStringValue_("")
            field.setPlaceholderString_(
                "already set — leave blank to keep" if os.getenv(name) else name)

    def saveKeys_(self, sender):
        values = {name: field.stringValue().strip()
                  for name, field in self.key_fields.items()}
        values = {k: v for k, v in values.items() if v}
        if values:
            config.write_env(values, replace=True)
            # The process already loaded .env at import, so the new keys have to
            # go into the environment too or nothing works until a restart.
            os.environ.update(values)
        self.keys_window.orderOut_(None)
        self._rebuild_menu()

    def closeKeys_(self, sender):
        self.keys_window.orderOut_(None)

    # --- dock visibility ----------------------------------------------------

    def toggleDock_(self, sender):
        show = not config.load().get("show_in_dock", False)
        config.save({"show_in_dock": show})
        apply_dock_policy(show)
        if show:
            # Without this the Dock tile appears but the app stays behind
            # whatever was in front, which reads as nothing having happened.
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def quit_(self, sender):
        self._stop.set()
        NSApplication.sharedApplication().terminate_(None)

    # --- the daily run ------------------------------------------------------

    @objc.python_method
    def _start_run(self, reason: str) -> None:
        with db.session(web.app.config["DB"]) as conn:
            handles = discovery.approved_handles(conn)
        if not handles:
            # fetch_recent([]) still bills a run, so never start an empty one.
            print("no approved accounts; nothing to fetch", file=sys.stderr)
            return
        err = web.start_fetch(handles, f"{len(handles)} accounts ({reason})")
        if err:
            print(f"fetch not started: {err}", file=sys.stderr)

    @objc.python_method
    def _schedule_loop(self):
        """Run when one is overdue, re-checking on a short cycle.

        Deliberately not a wall-clock timer: see `scheduler`. Sleeping in short
        slices means a laptop that wakes at 09:00 notices within minutes rather
        than at the next scheduled hour.
        """
        while not self._stop.is_set():
            try:
                with db.session(web.app.config["DB"]) as conn:
                    overdue = scheduler.due(conn)
                if overdue and web.JOB["state"] != "running":
                    self._start_run("scheduled")
            except Exception as exc:
                # A scheduler thread that dies takes the daily run with it and
                # says nothing, which is the worst available outcome.
                print(f"scheduler: {type(exc).__name__}: {exc}", file=sys.stderr)
            self._stop.wait(scheduler.CHECK_EVERY_SECONDS)


def main() -> None:
    paths.ensure()
    port = _pick_port()
    web.app.config["DB"] = str(db.DB_PATH)

    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    # Let the server bind before the webview asks for a page.
    time.sleep(0.4)

    app = NSApplication.sharedApplication()
    # Accessory: menu bar only, no Dock icon and no menu bar takeover.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().initWithPort_(port)
    app.setDelegate_(delegate)
    print(f"serving http://localhost:{port}  ICS: http://localhost:{port}/calendar.ics")
    app.run()


if __name__ == "__main__":
    main()
