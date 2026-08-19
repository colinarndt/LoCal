"""The menu bar app: status item, embedded calendar, self-scheduling daily run.

    python -m local_calendar.app

Written straight against AppKit rather than on rumps + pywebview. Both of those
want to own the main thread's NSApplication run loop, and there is exactly one;
combining them is a fight, and freezing pywebview into a bundle adds a
hidden-import tail on top. A status item and a WKWebView in a window is what
those libraries wrap anyway, and doing it directly costs two dependencies
instead of four.

Three things share this process:

  * Flask, on a daemon thread, serving the same UI as `python -m local_calendar.web`
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
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                    NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
                    NSButton, NSFont, NSImage, NSMenu, NSMenuItem, NSObject,
                    NSAlert, NSScreen, NSSecureTextField, NSTextField,
                    NSApplicationActivateAllWindows, NSApplicationActivateIgnoringOtherApps,
                    NSRunningApplication,
                    NSUserNotificationCenter, NSVariableStatusItemLength, NSStatusBar, NSWindow,
                    NSWorkspace,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from EventKit import EKEntityTypeEvent, EKEvent, EKEventStore, EKSpanThisEvent
from Foundation import NSDate, NSMakeRect, NSTimer, NSURL, NSURLRequest
from WebKit import (WKNavigationActionPolicyAllow, WKNavigationActionPolicyCancel,
                    WKWebView, WKWebViewConfiguration)

from . import config, db, discovery, paths, scheduler, spend, trips, web

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


def _install_main_menu(app, delegate) -> None:
    """Install the standard responder-chain editing shortcuts.

    AppKit does not synthesize these for a programmatic menu-bar application.
    Text fields and WKWebView still know how to perform ``copy:``/``paste:``,
    which is why their context menus work, but Command-key equivalents are only
    dispatched when matching items exist in the application's main menu.
    Targets stay nil so AppKit sends each action to the focused text control.
    """
    main = NSMenu.alloc().initWithTitle_("Main Menu")

    app_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "LoCal", None, "")
    main.addItem_(app_root)
    app_menu = NSMenu.alloc().initWithTitle_("LoCal")
    app_root.setSubmenu_(app_menu)
    settings = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Settings…", "showSettings:", ",")
    settings.setTarget_(delegate)
    app_menu.addItem_(settings)
    app_menu.addItem_(NSMenuItem.separatorItem())
    # This is normally supplied by a stock app menu. LoCal builds its own so
    # the editing shortcuts work in WKWebView, which means we add Cmd-H too.
    app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Hide LoCal", "hide:", "h"))
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit LoCal", "terminate:", "q"))

    edit_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Edit", None, "")
    main.addItem_(edit_root)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    edit_root.setSubmenu_(edit)
    for title, action, key in (
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key))

    app.setMainMenu_(main)


_EVENT_ICS_PATH = re.compile(r"^/event/(\d+)/calendar\.ics$")


class ExternalLinkDelegate(NSObject):
    """Open external links, while adding LoCal events natively on macOS."""

    def initWithAppDelegate_(self, app_delegate):
        self = objc.super(ExternalLinkDelegate, self).init()
        if self is None:
            return None
        self.app_delegate = app_delegate
        return self

    def webView_decidePolicyForNavigationAction_decisionHandler_(
            self, webview, action, decision_handler):
        target_frame = action.targetFrame()
        url = action.request().URL()
        scheme = str(url.scheme() or "").lower() if url else ""
        if self.app_delegate.needs_api_key_prompt(url):
            self.app_delegate.prompt_for_api_keys(str(url.absoluteString()))
            decision_handler(WKNavigationActionPolicyCancel)
            return
        if target_frame is None and scheme in {"http", "https", "mailto"}:
            path = str(url.path() or "") if url else ""
            event = _EVENT_ICS_PATH.fullmatch(path)
            if (event and scheme == "http" and str(url.host() or "") == "127.0.0.1"
                    and int(url.port() or 80) == self.app_delegate.port):
                self.app_delegate.add_event_to_calendar(int(event.group(1)))
                decision_handler(WKNavigationActionPolicyCancel)
                return
            NSWorkspace.sharedWorkspace().openURL_(url)
            decision_handler(WKNavigationActionPolicyCancel)
            return
        decision_handler(WKNavigationActionPolicyAllow)


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
        self.link_delegate = None
        self.keys_window = None
        self.key_fields = {}
        self.event_store = EKEventStore.alloc().init()
        # PyObjC marks EventKit's completion block as unretained, so retain it
        # until the asynchronous permission prompt has answered.
        self.calendar_access_callbacks = []
        self._stop = threading.Event()
        return self

    # --- lifecycle ----------------------------------------------------------

    def applicationDidFinishLaunching_(self, notification):
        NSUserNotificationCenter.defaultUserNotificationCenter().setDelegate_(self)
        bar = NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        button = self.status_item.button()
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "calendar", "LoCal")
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

        # Website calendars work without API keys. Instagram credentials are
        # requested only after someone actually adds an Instagram account.

    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        # Closing the calendar window hides it; the app lives in the menu bar.
        return False

    def userNotificationCenter_didActivateNotification_(self, center, notification):
        """Open the ticket target when the user presses a performer alert."""
        info = notification.userInfo()
        ticket_url = info.objectForKey_("ticket_url") if info else None
        if ticket_url:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(ticket_url))

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
        self.menu.addItem_(self._item("Settings…", "showSettings:"))
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
                websites = conn.execute(
                    "SELECT COUNT(*) FROM web_source WHERE enabled=1").fetchone()[0]
        except Exception as exc:
            return [f"database unavailable: {type(exc).__name__}"]

        lines = [f"Last run: {last}",
                 f"Watching {polled} accounts, {websites} websites"]

        absent = missing_keys() if polled else []
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

        lines.append(f"Last 24h: {_money(totals['last_24h'])}")
        # Never labelled "all time": this ledger postdates the install, so spend
        # before it started cannot be reconstructed. Say when counting began
        # instead of showing a number that quietly understates the real total.
        lines.append(f"Since {web.short_date(totals['since'])}: {_money(totals['all_time'])} "
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
        # WKWebView ignores target=_blank by default. Keep a strong reference
        # to the delegate, then pass external pages and ticket links to macOS.
        self.link_delegate = ExternalLinkDelegate.alloc().initWithAppDelegate_(self)
        self.webview.setNavigationDelegate_(self.link_delegate)
        self.webview.setAutoresizingMask_(1 << 1 | 1 << 4)   # width | height
        self.webview.loadRequest_(
            NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))
        self.window.contentView().addSubview_(self.webview)

    def fetchNow_(self, sender):
        self._start_run("menu bar")

    @objc.python_method
    def needs_api_key_prompt(self, url) -> bool:
        """Whether the embedded Sources page requested the native key window."""
        if url is None or str(url.scheme() or "").lower() != "http":
            return False
        if (str(url.host() or "") != "127.0.0.1"
                or int(url.port() or 80) != self.port
                or str(url.path() or "") != "/discover"):
            return False
        return ("need_keys", "1") in parse_qsl(str(url.query() or ""),
                                                   keep_blank_values=True)

    @objc.python_method
    def prompt_for_api_keys(self, url: str) -> None:
        """Open native key entry once, then remove the one-shot URL marker."""
        parsed = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(
            parsed.query, keep_blank_values=True)
                 if not (key == "need_keys" and value == "1")]
        clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                urlencode(query), parsed.fragment))
        self.webview.loadRequest_(NSURLRequest.requestWithURL_(
            NSURL.URLWithString_(clean_url)))
        self.showKeys_(None)

    def showSettings_(self, sender):
        if self.window is None:
            self._build_window()
        self.webview.loadRequest_(NSURLRequest.requestWithURL_(
            NSURL.URLWithString_(self.url + "settings")))
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    # --- Calendar -----------------------------------------------------------

    @objc.python_method
    def add_event_to_calendar(self, event_id: int) -> None:
        """Ask Calendar permission if needed, then add one LoCal event.

        The web UI uses this same event representation to generate its ICS
        download. The app replaces that download with EventKit so clicking the
        shared button never leaves a temporary file behind on a Mac.
        """
        event = web.calendar_event_fields(event_id)
        if event is None:
            self._show_calendar_error("That event is no longer available.")
            return

        def complete(granted, error):
            try:
                if granted:
                    self.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "saveCalendarEvent:", event, False)
                else:
                    detail = str(error) if error else "Calendar access was not granted."
                    self.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "showCalendarError:", detail, False)
            finally:
                self.calendar_access_callbacks.remove(complete)

        # macOS 14 calls this "full access". Keep the older API for app builds
        # running on earlier supported versions of macOS.
        self.calendar_access_callbacks.append(complete)
        request_full = getattr(self.event_store, "requestFullAccessToEventsWithCompletion_", None)
        if request_full is not None:
            request_full(complete)
        else:
            self.event_store.requestAccessToEntityType_completion_(EKEntityTypeEvent, complete)

    def saveCalendarEvent_(self, event):
        try:
            native = EKEvent.eventWithEventStore_(self.event_store)
            native.setTitle_(event["title"])
            native.setStartDate_(self._calendar_date(event["start"]))
            native.setEndDate_(self._calendar_date(event["end"]))
            native.setAllDay_(event["all_day"])
            native.setLocation_(event["location"])
            native.setNotes_(event["notes"])
            if event["url"]:
                native.setURL_(NSURL.URLWithString_(event["url"]))
            calendar = self.event_store.defaultCalendarForNewEvents()
            if calendar is None:
                raise RuntimeError("No writable default calendar is configured.")
            native.setCalendar_(calendar)
            if not self.event_store.saveEvent_span_commit_error_(
                    native, EKSpanThisEvent, True, None):
                raise RuntimeError("Calendar did not save the event.")
            self._show_calendar_app()
        except Exception as exc:
            self._show_calendar_error(f"Could not add the event: {exc}")

    @objc.python_method
    def _calendar_date(self, value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=config.tzinfo())
        return NSDate.dateWithTimeIntervalSince1970_(value.timestamp())

    @objc.python_method
    def _show_calendar_app(self) -> None:
        """Bring the destination app forward so a successful add is visible."""
        workspace = NSWorkspace.sharedWorkspace()
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.apple.iCal")
        if not apps:
            if not workspace.launchApplication_("Calendar"):
                raise RuntimeError("Calendar could not be opened.")
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                "com.apple.iCal")
        for app in apps:
            app.activateWithOptions_(
                NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps)

    def showCalendarError_(self, detail):
        self._show_calendar_error(str(detail))

    @objc.python_method
    def _show_calendar_error(self, detail: str) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Could not add this event to Calendar")
        alert.setInformativeText_(detail)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

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
    def _start_run(self, reason: str, website_ids=None, include_accounts: bool = True) -> None:
        with db.session(web.app.config["DB"]) as conn:
            handles = discovery.approved_handles(conn) if include_accounts else []
            if website_ids is None:
                website_ids = trips.pollable_source_ids(conn)
        if not handles and not website_ids:
            print("no followed sources; nothing to fetch", file=sys.stderr)
            return
        label = f"{len(handles)} accounts, {len(website_ids)} websites ({reason})"
        err = web.start_fetch(handles, label, website_ids)
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
                    cfg = config.load()
                    overdue = scheduler.due(
                        conn, cfg["instagram_refresh_hours"])
                    venue_ids = scheduler.due_venue_source_ids(
                        conn, cfg["venue_refresh_hours"])
                    performer_ids = scheduler.due_performer_source_ids(
                        conn, cfg["performer_refresh_hours"])
                if web.JOB["state"] != "running":
                    website_ids = venue_ids + performer_ids
                    if overdue or website_ids:
                        due_kinds = []
                        if overdue:
                            due_kinds.append("Instagram")
                        if venue_ids:
                            due_kinds.append("venue")
                        if performer_ids:
                            due_kinds.append("performer")
                        self._start_run(
                            f"scheduled {' + '.join(due_kinds)} check",
                            website_ids, include_accounts=overdue)
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
    _install_main_menu(app, delegate)
    print(f"serving http://localhost:{port}  ICS: http://localhost:{port}/calendar.ics")
    app.run()


if __name__ == "__main__":
    main()
