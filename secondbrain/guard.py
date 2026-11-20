"""Runtime enforcement of the privacy model using Python audit hooks (PEP 578).

Once armed, the process cannot:
  * open files for writing, or create, move or delete files;
  * connect to or resolve anything other than loopback addresses;
  * start processes or load native libraries through ctypes;
  * read or list anything in the notes folder outside the approved folders.

The hook runs before each operation and cannot be removed once installed.
This is an in-process control over the standard library (the app has no
third-party dependencies), not an operating-system sandbox.
"""
from __future__ import annotations

import os
import sys

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

# Filesystem changes that do not go through open().
_DISK_CHANGE_EVENTS = {
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.truncate",
    "os.chmod", "os.chown", "os.link", "os.symlink", "os.utime",
    "os.mkfifo", "os.mknod", "os.setxattr", "os.removexattr",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat",
    "shutil.copytree", "shutil.move", "shutil.rmtree", "shutil.make_archive",
    "shutil.unpack_archive", "tempfile.mkstemp", "tempfile.mkdtemp",
    "sqlite3.connect",
}

# Process creation and native code loading (would bypass the hook).
_PROCESS_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
    "os.fork", "os.forkpty", "os.startfile", "pty.spawn", "webbrowser.open",
    "ctypes.dlopen",
}

_NAME_LOOKUP_EVENTS = {
    "socket.gethostbyname", "socket.gethostbyaddr", "socket.getaddrinfo",
}


class GuardViolation(PermissionError):
    """Raised when a prohibited operation is attempted."""


class _State:
    armed = False
    notes_root = None      # normalised real path of the notes folder
    exempt_dirs = ()       # the app's own code/web folders
    is_allowed = None      # callable(real_path) -> bool, set by the session
    on_event = None        # callable(kind, detail), set by the ledger


_state = _State()


def _norm(path):
    try:
        return os.path.normcase(os.path.realpath(os.fspath(path)))
    except Exception:
        return None


def _inside(path, folder):
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _report(kind, detail):
    cb = _state.on_event
    if cb is not None:
        try:
            cb(kind, detail)
        except Exception:
            pass


def _block(what, detail):
    _report("blocked", {"what": what, "detail": str(detail)[:300]})
    raise GuardViolation("Blocked by the privacy guard: %s (%s)" % (what, str(detail)[:200]))


def _host_of(address):
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        return str(host)
    return None


def _in_notes(path):
    root = _state.notes_root
    if root is None or not _inside(path, root):
        return False
    for d in _state.exempt_dirs:
        if _inside(path, d):
            return False
    return True


def _check_read(path, how):
    """Reads and listings inside the notes folder require an approved folder."""
    if not _in_notes(path):
        return
    allowed = _state.is_allowed
    if allowed is None or not allowed(path):
        _block("read something you have not switched on", path)
    if how == "open":
        _report("read", {"path": path})


def _hook(event, args):
    if not _state.armed:
        return

    if event == "open":
        path, _mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(path, int):
            return                       # an already-open descriptor
        writing = bool(flags & _WRITE_FLAGS) if isinstance(flags, int) else (
            isinstance(_mode, str) and any(c in _mode for c in "wax+"))
        if writing:
            if os.fspath(path) == os.devnull:
                return
            _block("write to disk", path)
        p = _norm(path)
        if p:
            _check_read(p, "open")
        return

    if event in ("os.listdir", "os.scandir"):
        p = _norm(args[0] if args and args[0] is not None else ".")
        if p and p != _state.notes_root:
            _check_read(p, "list")
        return

    if event in _DISK_CHANGE_EVENTS:
        _block("change files on disk (%s)" % event, args[0] if args else "")

    if event in _PROCESS_EVENTS:
        _block("start another program (%s)" % event, args[0] if args else "")

    if event in ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind"):
        address = args[1] if len(args) > 1 else None
        if address is None and event == "socket.sendmsg":
            return                       # already-connected socket
        host = _host_of(address)
        if host is None or host not in LOOPBACK_HOSTS:
            what = "listen on the network" if event == "socket.bind" else "send data off this device"
            _block(what, address)
        if event == "socket.connect":
            _report("local_connection", {"port": address[1]})
        return

    if event in _NAME_LOOKUP_EVENTS:
        host = args[0] if args else None
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host is not None and str(host) not in LOOPBACK_HOSTS and str(host) != "":
            _block("look up an internet address", host)
        return


def arm(notes_root, exempt_dirs=(), is_allowed=None, on_event=None):
    """Install the audit hook. It cannot be disabled afterwards."""
    if _state.armed:
        raise RuntimeError("guard is already armed")
    sys.dont_write_bytecode = True
    _state.notes_root = _norm(notes_root)
    _state.exempt_dirs = tuple(d for d in (_norm(x) for x in exempt_dirs) if d)
    _state.is_allowed = is_allowed
    _state.on_event = on_event
    sys.addaudithook(_hook)
    _state.armed = True


def is_armed():
    return _state.armed


def self_test(notes_root, hidden_dir=None):
    """Attempt each prohibited operation and report whether it was blocked.

    The attempts are real; the guard is what stops them.
    """
    import socket
    results = []

    def attempt(label, fn):
        try:
            fn()
            results.append({"attempt": label, "blocked": False, "note": "NOT blocked. The lock failed."})
        except GuardViolation as e:
            results.append({"attempt": label, "blocked": True, "note": str(e)})
        except Exception as e:  # failed before reaching the guard
            results.append({"attempt": label, "blocked": True,
                            "note": "Failed before the lock was needed: %s" % e})

    def send_out():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(2)
            s.connect(("93.184.216.34", 443))   # any public address
        finally:
            s.close()

    def lookup():
        socket.getaddrinfo("example.com", 443)

    def write_copy():
        with open(os.path.join(notes_root, "_assistant_copy.tmp"), "w") as f:
            f.write("a copy of your notes")

    def write_elsewhere():
        with open(os.path.join(os.path.expanduser("~"), "second_brain_copy.tmp"), "w") as f:
            f.write("a copy of your notes")

    def launch():
        import subprocess
        subprocess.Popen(["curl", "https://example.com"])

    attempt("Send data to a server on the internet", send_out)
    attempt("Look up an internet address", lookup)
    attempt("Save a copy inside your notes folder", write_copy)
    attempt("Save a copy somewhere else on this computer", write_elsewhere)
    attempt("Start another program to do it instead", launch)
    if hidden_dir:
        attempt("Look inside a folder you switched off (%s)" % os.path.basename(hidden_dir),
                lambda: os.listdir(hidden_dir))
    return results
