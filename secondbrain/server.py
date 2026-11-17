"""A small web server that only this computer can reach (127.0.0.1)."""
from __future__ import annotations

import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from . import brief, guard

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/favicon.ico": ("icon.ico", "image/x-icon")}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".gif": "image/gif", ".bmp": "image/bmp"}
# The page may only load things from this server: no fonts, scripts or trackers from elsewhere.
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):                       # skip HTTPServer's host-name lookup
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_handler(session, ledger):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # -- helpers --
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")          # the browser keeps no copy either
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, events):
            """Send events one line at a time as they happen (chunked, so the page sees each at once)."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            def put(event):
                data = (json.dumps(event) + "\n").encode("utf-8")
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
            try:
                try:
                    for event in events:
                        put(event)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    raise
                except Exception as e:
                    put({"type": "final", "available": False, "reason": "Something went wrong: %s" % e})
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True     # the page moved on; stop the model writing
            finally:
                close = getattr(events, "close", None)
                if close:
                    close()

        def _local_only(self):
            """Refuse requests that did not come from this app's own page."""
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "this assistant only answers on this computer"})
                return False
            life = getattr(self.server, "lifeline", None)
            if life is not None and urlparse(self.path).path != "/api/ping":
                life.seen()
            return True

        def _state(self):
            s = session.state()
            s["model"] = session.model.refresh()
            s["receipt"] = ledger.snapshot(session.root)
            s["locked"] = guard.is_armed()
            return s

        # -- routes --
        def do_GET(self):
            if not self._local_only():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                with open(os.path.join(WEB, name), "rb") as f:
                    return self._send(200, f.read(), ctype)
            if url.path == "/api/ping":                  # lets a second double-click find this window
                return self._send(200, {"app": "second-brain"})
            if url.path == "/api/state":
                session.sync()
                return self._send(200, self._state())
            if url.path == "/api/source":
                doc = session.docs.get((q.get("id") or [""])[0])
                if not doc:
                    return self._send(404, {"error": "I no longer have that note in memory."})
                # (a note that failed to read still opens, so the reason can be shown)
                out = doc.public()
                out.update({"lines": doc.lines, "date_source": doc.date_source, "pages": len(doc.images or []),
                            "fingerprint": (doc.sha256 or "")[:16]})
                return self._send(200, out)
            if url.path == "/api/image":
                doc = session.docs.get((q.get("id") or [""])[0])
                if not doc or doc.kind != "image":
                    return self._send(404, {"error": "not found"})
                if doc.images:                       # a page of a scanned PDF, held in memory
                    try:
                        img = doc.images[int((q.get("page") or ["1"])[0]) - 1]
                    except (ValueError, IndexError):
                        return self._send(404, {"error": "no such page"})
                    return self._send(200, img, "image/jpeg" if img[:2] == b"\xff\xd8" else "image/png")
                try:
                    with open(doc.path, "rb") as f:
                        data = f.read()
                except OSError:
                    return self._send(404, {"error": "not available"})
                return self._send(200, data, IMAGE_MIME.get(os.path.splitext(doc.path)[1].lower(), "application/octet-stream"))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_only():
                return
            if urlparse(self.path).path == "/api/bye":          # the window is closing (or reloading)
                origin = self.headers.get("Origin") or ""
                if origin.split("://", 1)[-1] == self.headers.get("Host"):
                    life = getattr(self.server, "lifeline", None)
                    if life is not None:
                        life.bye()
                    return self._send(204, b"")
                return self._send(403, {"error": "request did not come from the app"})
            if self.headers.get("X-Second-Brain") != "1":      # other websites cannot set this header
                return self._send(403, {"error": "request did not come from the app"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._send(400, {"error": "bad request"})
            path = urlparse(self.path).path
            try:
                if path == "/api/allow":
                    session.set_allowed(str(body.get("folder")), bool(body.get("on")))
                    return self._send(200, self._state())
                if path == "/api/allow_all":
                    for u in session.units():
                        if (u in session.allowed) != bool(body.get("on")):
                            session.set_allowed(u, bool(body.get("on")))
                    return self._send(200, self._state())
                if path == "/api/hide":
                    session.set_hidden(str(body.get("note")), bool(body.get("hide")))
                    return self._send(200, self._state())
                if path == "/api/prep":
                    return self._send(200, brief.prep(session, body.get("query")))
                if path == "/api/prep/ai":
                    return self._send(200, brief.prep_ai(session, body.get("query")))
                if path == "/api/ask":
                    return self._send(200, brief.ask(session, body.get("question")))
                if path == "/api/ask/ai":
                    return self._send(200, brief.ask_ai(session, body.get("question")))
                if path in ("/api/prep/ai/stream", "/api/ask/ai/stream"):
                    events = (brief.prep_ai_events(session, body.get("query")) if "prep" in path
                              else brief.ask_ai_events(session, body.get("question")))
                    return self._stream(events)
                if path == "/api/selftest":
                    off = [u for u in session.units() if u not in session.allowed and not u.startswith("(")]
                    hidden_dir = os.path.join(session.root, off[0]) if off else None
                    results = guard.self_test(session.root, hidden_dir)
                    ledger.record("selftest", {"attempts": len(results),
                                               "blocked": sum(1 for r in results if r["blocked"])})
                    return self._send(200, {"results": results, "state": self._state()})
                if path == "/api/end":
                    receipt = session.end()
                    return self._send(200, {"receipt": receipt, "state": self._state()})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(session, ledger, port):
    last = None
    for p in range(port, port + 20):
        try:
            httpd = Server(("127.0.0.1", p), make_handler(session, ledger))
            return httpd, p
        except OSError as e:
            last = e
    raise last
