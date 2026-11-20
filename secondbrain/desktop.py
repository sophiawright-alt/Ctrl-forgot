"""Desktop-app behaviour for --app mode.

Opens the UI in a browser app-mode window, reuses a running instance of the
same version, and shuts down when the window closes. The window is launched
before the guard is armed; afterwards the process cannot start programs.
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

# Shutdown timings (seconds); overridable for tests.
BYE_GRACE = float(os.environ.get("SECOND_BRAIN_BYE_GRACE", "5"))         # wait after a close beacon (reloads send one too)
IDLE_LIMIT = float(os.environ.get("SECOND_BRAIN_IDLE_LIMIT", "240"))      # no requests at all
FIRST_VISIT = float(os.environ.get("SECOND_BRAIN_FIRST_VISIT", "120"))    # window never connected


def read_settings(path):
    """Parse settings.txt: `name = value` lines, `#` comments."""
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
    """Address of a running instance of this version on `port`, else None."""
    from . import __version__
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        conn.request("GET", "/api/ping")
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        if data.get("app") == "second-brain" and data.get("version") == __version__:
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
    """Open the UI in an app-mode window if possible. Returns how it was opened."""
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


def tell(message, title="Ctrl+Forgot"):
    """Show a message box (no console is available in --app mode)."""
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
    """Shuts the app down once its window is gone.

    The page sends a beacon on close. A reload also sends one, so shutdown waits
    BYE_GRACE seconds for the page to return. Without any requests for IDLE_LIMIT
    seconds (e.g. a crashed window), the app also exits.
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
