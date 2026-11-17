"""The receipt.

A running, in-memory record of everything the assistant touched this session:
which notes it read, each time it spoke to the model on this machine, and
every attempt the lock stopped. Entries about reads and connections come from
the lock itself (guard.py), not from the code doing the reading, so the app
cannot forget to mention something.

Like everything else here it lives only in memory and is gone when the
session ends.
"""
from __future__ import annotations

import threading
import time


class Ledger:
    MAX_EVENTS = 400

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        with self._lock:
            self.events = []
            self.files_read = {}        # path -> times read
            self.local_connections = 0
            self.model_calls = 0
            self.blocked = 0
            self.seq = 0

    def record(self, kind, detail=None):
        detail = dict(detail or {})
        with self._lock:
            if kind == "read":
                path = detail.get("path", "")
                n = self.files_read.get(path, 0)
                self.files_read[path] = n + 1
                if n:                   # only the first read of a file is news
                    return
            elif kind == "local_connection":
                self.local_connections += 1
                return                  # counted; model calls are logged separately
            elif kind == "model":
                self.model_calls += 1
            elif kind == "blocked":
                self.blocked += 1
            self.seq += 1
            self.events.append({"n": self.seq, "t": time.time(), "kind": kind, "detail": detail})
            if len(self.events) > self.MAX_EVENTS:
                del self.events[: len(self.events) - self.MAX_EVENTS]

    def snapshot(self, root=None, limit=60):
        with self._lock:
            def short(p):
                if root and p.startswith(root):
                    return p[len(root):].lstrip("\\/")
                return p
            events = []
            for e in self.events[-limit:]:
                d = dict(e["detail"])
                if "path" in d:
                    d["path"] = short(d["path"])
                events.append({"n": e["n"], "t": e["t"], "kind": e["kind"], "detail": d})
            return {
                "files_read": len(self.files_read),
                "files": sorted(short(p) for p in self.files_read),
                "model_calls": self.model_calls,
                "local_connections": self.local_connections,
                "blocked": self.blocked,
                # These two are zero by construction: the lock raises before
                # either can happen. They are shown so the user sees the claim.
                "sent_off_device": 0,
                "written_to_disk": 0,
                "events": events,
            }
