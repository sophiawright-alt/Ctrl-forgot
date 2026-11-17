"""Making Second Brain behave like a desktop app.

Double-click the desktop icon (or "Second Brain.pyw") and:

  * the app starts with no terminal window;
  * it opens in its own window (Edge or Chrome "app" mode: no tabs, no
    address bar), or the normal browser if neither is installed;
  * closing that window ends the session: everything in memory is
    forgotten and the app quits;
  * double-clicking again while it is running just opens the window again.

Opening the window means starting another program, which the lock forbids.
So it happens once, before the lock is switched on and before any note has
been read. After that the app cannot start anything.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time

# How long to wait before quitting. Overridable for the automated tests.
BYE_GRACE = float(os.environ.get("SECOND_BRAIN_BYE_GRACE", "5"))         # a reload looks like a close at first
IDLE_LIMIT = float(os.environ.get("SECOND_BRAIN_IDLE_LIMIT", "240"))      # window gone without saying so
FIRST_VISIT = float(os.environ.get("SECOND_BRAIN_FIRST_VISIT", "120"))    # window never opened


def read_settings(path):
    """settings.txt: 'name = value' lines; '#' starts a comment."""
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip().lower()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def already_running(port):
    """If Second Brain is already running on this port, return its address."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        conn.request("GET", "/api/ping")
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        if data.get("app") == "second-brain":
            return "http://127.0.0.1:%d" % port
    except (OSError, ValueError, http.client.HTTPException):
        pass
    return None


def _candidates():
    if sys.platform.startswith("win"):
        roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for root in filter(None, roots):
            yield os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe")
            yield os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            yield "/Applications/%s.app/Contents/MacOS/%s" % (app, app)
            yield os.path.expanduser("~/Applications/%s.app/Contents/MacOS/%s" % (app, app))
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                yield found


def open_window(url):
    """Open the app in its own window. Returns a short description of how."""
    if os.environ.get("SECOND_BRAIN_NO_WINDOW"):
        return "not opened (test mode)"
    flags = 0
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    for exe in _candidates():
        if os.path.isfile(exe):
            try:
                subprocess.Popen([exe, "--app=" + url, "--window-size=1440,920", "--new-window"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True, creationflags=flags)
                return "app window (%s)" % os.path.basename(exe)
            except OSError:
                continue
    import webbrowser
    webbrowser.open(url, new=1)
    return "default browser"


def tell(message, title="Second Brain"):
    """Show a message to someone who has no terminal to read it in."""
    if os.environ.get("SECOND_BRAIN_NO_WINDOW"):
        print(message)
        return
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
            return
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'display dialog %s with title %s buttons {"OK"}' % (
                json.dumps(message), json.dumps(title))], check=False)
            return
    except Exception:
        pass
    print(message)


class Lifeline:
    """Quits the app once its window has gone.

    The page says goodbye when it closes. Because a reload says goodbye too,
    the app waits a few seconds to see whether the page comes back. If the
    window disappears without a word (a crash), the app quits after a few
    minutes of silence.
    """

    def __init__(self, stop):
        self.stop = stop
        self.started = time.time()
        self.last_seen = None
        self.bye_at = None
        self._lock = threading.Lock()
        threading.Thread(target=self._watch, daemon=True).start()

    def seen(self):
        with self._lock:
            self.last_seen = time.time()

    def bye(self):
        with self._lock:
            self.bye_at = time.time()

    def _watch(self):
        while True:
            time.sleep(0.5)
            now = time.time()
            with self._lock:
                last, bye = self.last_seen, self.bye_at
            if last is None:
                if now - self.started > FIRST_VISIT:
                    break
                continue
            if bye is not None and last <= bye and now - bye > BYE_GRACE:
                break
            if now - last > IDLE_LIMIT:
                break
        self.stop()
