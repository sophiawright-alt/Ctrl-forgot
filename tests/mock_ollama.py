"""A stand-in for Ollama, for tests only. Listens on 127.0.0.1 and gives canned replies."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TRANSCRIPTS = {
    "notebook": {"kind": "handwriting", "text": "Tues 10 Nov - corridor chat w/ Priya\n\nP wants to LEAD the Helix pilot\nsaid yes in principle !!\n\nbut only if backfill sorted\n-> ask Dan, 2 days/wk from Kofi?\n\nconference: tell her YES\n(budget ok - Mei confirmed £600[?])\n\ndo before 1:1 on the 16th"},
    "chat": {"kind": "screenshot", "text": "Dan Okafor  Thu 12 Nov\nYou: any chance of backfill for Priya so she can lead the pilot?\nDan: Not full time. Headcount freeze holds until January.\nDan: I can give you Kofi 2 days a week from 1 Dec.\nYou: that might be enough. can you confirm by Monday?\nDan: Will confirm Mon 16 Nov after I speak to his manager."},
}
SEEN = []
DELAY = float(__import__('os').environ.get('MOCK_DELAY', '0.004'))
STOPPED = []


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._send({"models": [{"name": "qwen3-vl:8b"}, {"name": "nomic-embed-text:latest"},
                                          {"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com"}]})
        self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        SEEN.append((self.path, body.get("model")))
        if self.path == "/api/show":
            caps = ["embedding"] if "embed" in body["model"] else ["completion", "vision"]
            return self._send({"capabilities": caps})
        if self.path == "/api/embed":
            vecs = []
            for t in body.get("input", []):
                v = [0.0] * 64
                for w in t.lower().split():
                    v[hash(w.strip(".,?!:;()[]")) % 64] += 1.0
                vecs.append(v)
            return self._send({"embeddings": vecs})
        if self.path == "/api/generate":
            return self._send({"done": True})
        if self.path == "/api/chat":
            msg = body["messages"][-1]
            if msg.get("images"):
                size = len(msg["images"][0])
                out = TRANSCRIPTS["notebook"] if size > 100000 else TRANSCRIPTS["chat"]
            elif "Question:" in msg["content"]:
                out = {"found": True, "caveat": "", "answer": [
                    {"point": "The Helix budget was cut to £28k for year one on 3 Nov, down from £40k agreed on 6 Oct.", "sources": [1, 2]},
                    {"point": "The board has approved a further £500k for 2027.", "sources": [1]},      # invented: must be dropped
                    {"point": "Everyone is very happy.", "sources": []}]}                               # unsourced: must be dropped
            else:
                out = {"headline": "Priya wants to lead the Helix pilot.",
                       "talking_points": [{"point": "Priya wants to lead the Helix pilot and backfill is not sorted.", "sources": [1, 2, 3]},
                                          {"point": "Unicorns will deliver the quarterly synergy roadmap.", "sources": [1]}],
                       "watch_outs": [{"point": "No source for this one.", "sources": [99]}],
                       "gaps": ["Whether Dan has confirmed Kofi."]}
            text = json.dumps(out)
            if body.get("stream"):                   # like Ollama: one JSON line per few characters
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for i in range(0, len(text), 9):
                        line = {"message": {"role": "assistant", "content": text[i:i + 9]}, "done": False}
                        self.wfile.write((json.dumps(line) + "\n").encode())
                        self.wfile.flush()
                        time.sleep(DELAY)
                    self.wfile.write((json.dumps({"message": {"content": ""}, "done": True}) + "\n").encode())
                except (BrokenPipeError, ConnectionResetError):
                    STOPPED.append(True)
                return
            return self._send({"message": {"role": "assistant", "content": text}})
        self._send({}, 404)


def start(port=0):
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    s = start(int(sys.argv[1]) if len(sys.argv) > 1 else 11434)
    print("mock ollama on", s.server_address[1], flush=True)
    threading.Event().wait()
