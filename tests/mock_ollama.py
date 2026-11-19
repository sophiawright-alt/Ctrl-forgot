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
THINKERS = set()
MODE = {'think': 'none'}   # none | think (reasons first) | empty (silent when think=false)
LOADED = set()
PROMPTS = []
WARMS = []
CHATS = []
MODELS = {"qwen3-vl:8b": "8.8B", "nomic-embed-text:latest": "137M"}


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
            return self._send({"models": [{"name": n, "details": {"parameter_size": MODELS[n]}} for n in MODELS] +
                                         [{"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com"}]})
        if self.path == "/api/ping":                     # pretends to be an older Second Brain
            return self._send({"app": "second-brain", "version": "0.1.0"})
        if self.path == "/api/ps":
            return self._send({"models": [{"name": n} for n in sorted(LOADED)]})
        self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        SEEN.append((self.path, body.get("model")))
        if self.path == "/api/show":
            caps = ["embedding"] if "embed" in body["model"] else ["completion", "vision"]
            if body["model"] in THINKERS:
                caps.append("thinking")
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
            WARMS.append((body.get("options") or {}).get("num_ctx"))
            if body.get("keep_alive") == 0:
                LOADED.discard(body.get("model"))
            else:
                LOADED.add(body.get("model"))
            return self._send({"done": True})
        if self.path == "/api/chat":
            LOADED.add(body.get("model"))
            CHATS.append((body.get("model"), body.get("options", {}), sum(len(m["content"]) for m in body["messages"])))
            PROMPTS.append(body["messages"])
            msg = body["messages"][-1]
            if msg.get("images"):
                size = len(msg["images"][0])
                out = dict(TRANSCRIPTS["notebook"] if size > 100000 else TRANSCRIPTS["chat"])
                if MODE.get("label") == "no_text":
                    out["kind"] = "no_text"
                if not body.get("format"):                  # asked plainly: a plain-text reply, like Ollama
                    reply = out["text"]
                    if MODE.get("label") == "fenced":
                        reply = "```\n" + reply + "\n```"
                    elif MODE.get("label") == "json":
                        reply = json.dumps(out)
                    elif MODE.get("label") == "blank":
                        reply = "NO TEXT"
                    return self._send({"message": {"role": "assistant", "content": reply}})
            elif "Question:" in msg["content"] and MODE.get("ask") == "unsure":   # small-model habits
                out = {"found": False, "caveat": "", "answer": [
                    {"point": "The Helix pilot start moved to 1 Dec, with 2 teams not 4.", "sources": ["1", "[2]"]}]}
            elif "Question:" in msg["content"] and MODE.get("ask") == "refuse":   # says no, gives nothing
                out = {"found": False, "caveat": "", "answer": []}
            elif "Question:" in msg["content"] and MODE.get("ask") == "sloppy":   # wrong labels, no sources
                out = {"found": "true", "caveat": "", "answer": [{"text": "The Helix pilot start moved to 1 Dec."},
                                                                 "The Helix pilot now has 2 teams not 4."]}
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
            if MODE["think"] == "empty" and body.get("think") is False:
                text = ""
            if body.get("stream"):                   # like Ollama: one JSON line per few characters
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    if MODE["think"] == "think":
                        for k in range(5):
                            line = {"message": {"role": "assistant", "content": "", "thinking": "hmm, let me see. "}, "done": False}
                            self.wfile.write((json.dumps(line) + "\n").encode())
                            self.wfile.flush()
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
    MODE["ask"] = __import__("os").environ.get("MOCK_ASK", "normal")
    s = start(int(sys.argv[1]) if len(sys.argv) > 1 else 11434)
    print("mock ollama on", s.server_address[1], flush=True)
    threading.Event().wait()
