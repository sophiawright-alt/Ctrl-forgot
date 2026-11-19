"""Talking to a model that runs on this machine (Ollama).

The address is fixed to 127.0.0.1. It is not configurable, and the lock in
guard.py would refuse anything else anyway. Models that Ollama would run in
the cloud are filtered out and never used.

Every request is stateless: we send the excerpts needed for one answer and
keep nothing. At the end of a session `unload()` asks Ollama to drop the model
from memory too.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import threading
import time

HOST = "127.0.0.1"

# Preference order when several local models are installed (first match wins).
VISION_PREFERENCE = ["qwen3-vl", "qwen3.5", "gemma4", "qwen2.5vl", "gemma3", "llama3.2-vision",
                     "minicpm-v", "mistral-small", "llava", "moondream"]
VISION_HINTS = VISION_PREFERENCE + ["vision", "-vl", "vl:", "ocr", "pixtral", "bakllava"]
EMBED_HINTS = ["embed", "minilm", "bge-", "bge:", "e5-"]


class ModelError(Exception):
    pass


class LocalModel:
    def __init__(self, ledger, port=11434):
        self.ledger = ledger
        self.port = int(port)
        self._lock = threading.Lock()
        self._caps = {}
        self._no_think = set()
        self._sizes = {}
        self._warmed = {}
        self._checked = 0.0
        self.status = {"running": False, "chat": None, "vision": None, "embed": None,
                       "models": [], "cloud_models_ignored": [], "error": "not checked yet"}

    # ---- plumbing -------------------------------------------------------
    def _request(self, method, path, body=None, timeout=20):
        conn = http.client.HTTPConnection(HOST, self.port, timeout=timeout)
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"error": raw[:300]}
            if resp.status >= 400:
                raise ModelError(parsed.get("error") or "local model returned HTTP %d" % resp.status)
            return parsed
        except (OSError, http.client.HTTPException) as e:
            raise ModelError("cannot reach the local model: %s" % e)
        finally:
            conn.close()

    def _capabilities(self, name):
        if name not in self._caps:
            try:
                info = self._request("POST", "/api/show", {"model": name})
                caps = info.get("capabilities")
            except ModelError:
                caps = None
            if not caps:                         # older Ollama: guess from the name
                low = name.lower()
                caps = ["embedding"] if any(h in low for h in EMBED_HINTS) else ["completion"]
                if any(h in low for h in VISION_HINTS):
                    caps.append("vision")
            self._caps[name] = set(caps)
        return self._caps[name]

    # ---- discovery ------------------------------------------------------
    def refresh(self, force=False):
        with self._lock:
            if not force and time.time() - self._checked < 8:
                return self.status
            self._checked = time.time()
            try:
                tags = self._request("GET", "/api/tags", timeout=3)
            except ModelError as e:
                self.status = {"running": False, "chat": None, "vision": None, "embed": None,
                               "models": [], "cloud_models_ignored": [], "error": str(e)}
                return self.status
            local, cloud = [], []
            for m in tags.get("models", []):
                name = m.get("name") or m.get("model") or ""
                if m.get("remote_host") or m.get("remote_model") or "cloud" in name.lower():
                    cloud.append(name)           # would run off this device: never used
                else:
                    local.append(name)
                    self._sizes[name] = _billions(name, (m.get("details") or {}).get("parameter_size"))
            vision = [n for n in local if "vision" in self._capabilities(n)]
            chat = [n for n in local if "completion" in self._capabilities(n) and "ocr" not in n.lower()]
            embed = [n for n in local if "embedding" in self._capabilities(n)]

            def prefer(names, order):
                for key in order:
                    for n in names:
                        if key in n.lower():
                            return n
                return names[0] if names else None

            size = lambda n: self._sizes.get(n) or 99.0

            def thinks(n):                       # reasons at length before answering: slow
                low = n.lower()
                if "instruct" in low:
                    return False
                return "thinking" in low or "thinking" in self._capabilities(n)
            # Reading images happens in the background, so it gets the most accurate model.
            v = os.environ.get("SECOND_BRAIN_VISION_MODEL") or prefer(
                sorted(vision, key=lambda n: (thinks(n), -size(n) if size(n) <= 12 else size(n))), VISION_PREFERENCE)
            # Summaries are written while you wait, so they get the quickest sensible one:
            # the smallest installed model of at least 3 billion parameters.
            sensible = (sorted((n for n in chat if size(n) >= 3), key=lambda n: (thinks(n), size(n))) or
                        sorted(chat, key=lambda n: (thinks(n), -size(n))))
            c = os.environ.get("SECOND_BRAIN_MODEL") or (sensible[0] if sensible else None)
            e = os.environ.get("SECOND_BRAIN_EMBED_MODEL") or prefer(embed, ["embeddinggemma", "nomic", "mxbai"])
            for chosen in (v, c, e):
                if chosen and chosen not in local:
                    v = v if v in local else None
                    c = c if c in local else None
                    e = e if e in local else None
            self.status = {"running": True, "chat": c, "vision": v, "embed": e, "models": local,
                           "cloud_models_ignored": cloud,
                           "error": None if c else "Ollama is running but has no local model installed"}
            return self.status

    # ---- calls ----------------------------------------------------------
    def chat_json(self, model, system, user, schema, images=None, purpose="", timeout=420):
        """One stateless question to the local model; returns parsed JSON."""
        if "qwen" in model.lower():
            user += "\n/no_think"
        message = {"role": "user", "content": user}
        if images:
            message["images"] = [base64.b64encode(b).decode("ascii") for b in images]
        body = {"model": model, "stream": False, "format": schema, "keep_alive": "30m",
                "messages": [{"role": "system", "content": system}, message],
                "options": {"temperature": 0.1, "num_ctx": self.context_for(model, reading_images=bool(images))}}
        if model not in self._no_think:
            body["think"] = False                # answer directly; skip "thinking" where supported
        sent = len(system) + len(user)
        t0 = time.time()
        try:
            try:
                resp = self._request("POST", "/api/chat", body, timeout=timeout)
            except ModelError as e:
                if "think" in str(e).lower() and "think" in body:
                    self._no_think.add(model)
                    del body["think"]
                    resp = self._request("POST", "/api/chat", body, timeout=timeout)
                else:
                    raise
        except ModelError as e:
            self.ledger.record("model", {"purpose": purpose, "model": model, "sent_chars": sent,
                                         "images": len(images or []), "error": str(e)[:160]})
            raise
        content = (resp.get("message") or {}).get("content") or ""
        if not content.strip() and "think" in body:   # some Ollama versions: nothing when thinking is off
            self._no_think.add(model)
            del body["think"]
            try:
                resp = self._request("POST", "/api/chat", body, timeout=timeout)
                content = (resp.get("message") or {}).get("content") or ""
            except ModelError:
                pass
        self.ledger.record("model", {"purpose": purpose, "model": model, "sent_chars": sent,
                                     "images": len(images or []), "received_chars": len(content),
                                     "seconds": round(time.time() - t0, 1)})
        return _parse_answer(content)

    IMAGE_CONTEXT = 8192                         # a photo alone can take a few thousand tokens

    def context_for(self, model, reading_images=False):
        """How much working memory (tokens) to ask Ollama for.

        Every request to a model must ask for the same amount, or Ollama reloads the model,
        and less working memory leaves more of the graphics card for the model itself.
        4096 tokens comfortably holds a brief's notes and answer. "Thinking" models
        reason at length first, so they get 8192.
        """
        # Reading a photo needs room for the photo: with too little, Ollama cuts the image off
        # and the model sees nothing. A model kept just for photos always gets enough. When one
        # model does both jobs, answers (which you wait for) keep the smaller size, and Ollama
        # briefly reloads the model when it switches to reading a photo in the background.
        st = self.status
        if reading_images or (model == st.get("vision") and model != st.get("chat")):
            return max(self.IMAGE_CONTEXT, self._chat_context(model))
        return self._chat_context(model)

    def _chat_context(self, model):
        env = os.environ.get("SECOND_BRAIN_CONTEXT")
        if env and env.isdigit():
            return int(env)
        low = model.lower()
        if "instruct" in low:
            return 4096
        if "thinking" in low or model in self._no_think or "thinking" in self._capabilities(model):
            return 8192
        return 4096

    def loaded(self, model):
        """Is this model already in memory (so the answer can start straight away)?"""
        try:
            ps = self._request("GET", "/api/ps", timeout=3)
        except ModelError:
            return True                          # unknown: do not claim it is loading
        names = {m.get("name") or m.get("model") for m in ps.get("models", [])}
        return model in names

    def warm(self, which="chat"):
        """Load a model into memory ahead of time, so the first answer is not slowed by loading.
        Sends no notes: it is an empty request."""
        name = self.refresh().get(which)
        if not name or time.time() - self._warmed.get(name, 0) < 20 * 60:
            return
        self._warmed[name] = time.time()
        try:
            self._request("POST", "/api/generate", {"model": name, "keep_alive": "30m",
                                                    "options": {"num_ctx": self.context_for(name)}}, timeout=300)
            self.ledger.record("note", {"message": "Loaded %s into memory on this device, ready for "
                                                   "questions. No notes were sent." % name})
        except ModelError:
            self._warmed.pop(name, None)

    def stream_json(self, model, system, user, schema, purpose="", timeout=900, every=0.12, max_chars=None):
        """Like chat_json, but yields the answer while the model is still writing it.

        Yields ("thinking", True) once if the model reasons before answering,
        ("partial", object) as the answer grows (at most every `every`
        seconds), where object is the best reading of the unfinished JSON,
        then ("done", object) once with the finished answer.

        `max_chars` stops reading once the answer itself is that long (the
        model's thinking does not count towards it). Stopping early, for that
        or because the user asked something else, closes the connection,
        which tells Ollama to stop writing.
        """
        if "qwen" in model.lower():
            user += "\n/no_think"                # Qwen's own switch: answer without reasoning first
        sent = len(system) + len(user)
        t0 = time.time()
        conn, content, thought, failed, finished, answer = None, "", 0, None, False, None
        try:
            for attempt in (1, 2):
                body = {"model": model, "stream": True, "format": schema, "keep_alive": "30m",
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "options": {"temperature": 0.1, "num_ctx": self.context_for(model)}}
                if model not in self._no_think:
                    body["think"] = False
                conn = http.client.HTTPConnection(HOST, self.port, timeout=timeout)
                try:
                    conn.request("POST", "/api/chat", body=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
                    resp = conn.getresponse()
                except (OSError, http.client.HTTPException) as e:
                    raise ModelError("cannot reach the local model: %s" % e)
                if resp.status >= 400:
                    try:
                        err = json.loads(resp.read().decode("utf-8", "replace") or "{}").get("error") or ""
                    except ValueError:
                        err = ""
                    conn.close()
                    if attempt == 1 and "think" in err.lower() and "think" in body:
                        self._no_think.add(model)
                        continue
                    raise ModelError(err or "local model returned HTTP %d" % resp.status)
                content, thought, last = "", 0, 0.0
                while True:
                    try:
                        line = resp.readline()
                    except (OSError, http.client.HTTPException) as e:
                        raise ModelError("the local model stopped part-way: %s" % e)
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("error"):
                        raise ModelError(str(msg["error"]))
                    m = msg.get("message") or {}
                    if m.get("thinking"):
                        if not thought:
                            yield "thinking", True
                        thought += len(m["thinking"])
                    piece = m.get("content") or ""
                    if piece:
                        content += piece
                        if time.time() - last >= every:
                            last = time.time()
                            draft = partial_json(content)
                            if isinstance(draft, dict):
                                yield "partial", draft
                        if max_chars and len(content) > max_chars:
                            break                    # long enough: stop the model and use what it wrote
                    if msg.get("done"):
                        break
                conn.close()
                if content.strip():
                    break
                # No answer at all. Some Ollama versions return nothing when thinking is
                # switched off; letting the model decide for itself usually fixes it.
                if attempt == 1 and "think" in body:
                    self._no_think.add(model)
                    continue
                raise ModelError("the model wrote no answer" + (
                    " (it spent its time thinking; a model whose name ends in -instruct avoids this)" if thought else ""))
            try:
                answer = _parse_answer(content)
            except ModelError:
                answer = partial_json(content)   # stopped at the length cap: keep what was written
                if not isinstance(answer, dict) or not answer:
                    raise ModelError("the model's answer was not in the expected form (it began: %r)" % content[:60])
            finished = True
        except ModelError as e:
            failed = e
            raise
        finally:
            if conn is not None:
                conn.close()
            detail = {"purpose": purpose, "model": model, "sent_chars": sent, "images": 0,
                      "received_chars": len(content), "seconds": round(time.time() - t0, 1),
                      "reply": content[:400]}
            if thought:
                detail["thinking_chars"] = thought
            if failed is not None:
                detail["error"] = str(failed)[:160]
            elif not finished:
                detail["error"] = "stopped early (you asked something else)"
            self.ledger.record("model", detail)
        yield "done", answer

    def transcribe(self, model, image, purpose="", timeout=900):
        """Read the text in one image, as plain text.

        Asked as a plain question, not in a fixed reply format: with a photo attached,
        a fixed format can stop some models reading the photo at all (they answer "no text").
        Returns "" if the image has no readable text.
        """
        user = TRANSCRIBE_ASK + ("\n/no_think" if "qwen" in model.lower() else "")
        body = {"model": model, "stream": False, "keep_alive": "30m",
                "messages": [{"role": "system", "content": TRANSCRIBE_SYSTEM},
                             {"role": "user", "content": user,
                              "images": [base64.b64encode(image).decode("ascii")]}],
                "options": {"temperature": 0.1, "num_ctx": self.context_for(model, reading_images=True)}}
        if model not in self._no_think:
            body["think"] = False
        t0 = time.time()
        detail = {"purpose": purpose, "model": model, "sent_chars": len(TRANSCRIBE_SYSTEM) + len(user), "images": 1}
        try:
            resp = self._request("POST", "/api/chat", body, timeout=timeout)
            text = (resp.get("message") or {}).get("content") or ""
            if not text.strip() and "think" in body:   # some Ollama versions: nothing when thinking is off
                self._no_think.add(model)
                del body["think"]
                resp = self._request("POST", "/api/chat", body, timeout=timeout)
                text = (resp.get("message") or {}).get("content") or ""
        except ModelError as e:
            detail["error"] = str(e)[:160]
            self.ledger.record("model", detail)
            raise
        detail.update({"received_chars": len(text), "seconds": round(time.time() - t0, 1), "reply": text[:400]})
        self.ledger.record("model", detail)
        return _clean_transcript(text)

    def embed(self, model, texts, purpose="meaning search"):
        resp = self._request("POST", "/api/embed", {"model": model, "input": texts, "keep_alive": "10m"}, timeout=120)
        vectors = resp.get("embeddings") or []
        if len(vectors) != len(texts):
            raise ModelError("embedding model returned the wrong number of vectors")
        self.ledger.record("model", {"purpose": purpose, "model": model,
                                     "sent_chars": sum(len(t) for t in texts), "images": 0})
        return vectors

    def unload(self):
        """Ask Ollama to drop our models from its memory."""
        dropped = []
        for name in {self.status.get("chat"), self.status.get("vision")} - {None}:
            try:
                self._request("POST", "/api/generate", {"model": name, "keep_alive": 0}, timeout=15)
                dropped.append(name)
            except ModelError:
                pass
        em = self.status.get("embed")
        if em:
            try:
                self._request("POST", "/api/embed", {"model": em, "input": [], "keep_alive": 0}, timeout=15)
                dropped.append(em)
            except ModelError:
                pass
        return dropped


def _billions(name, reported):
    """Model size in billions of parameters, from Ollama's details or the tag (":8b")."""
    for text in (reported or "", name.split(":")[-1]):
        m = re.search(r"(\d+(?:\.\d+)?)\s*([bm])\b", str(text).lower())
        if m:
            return float(m.group(1)) / (1000.0 if m.group(2) == "m" else 1.0)
    return None


def _clean_transcript(text):
    """Strip what models add around a transcription: code fences, a stray JSON wrapper, "no text"."""
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    if t.startswith("{") and t.endswith("}"):        # a model that answered in JSON anyway
        try:
            obj = json.loads(t)
            t = str(obj.get("text") or "") if isinstance(obj, dict) else t
        except ValueError:
            pass
    if re.fullmatch(r"(?i)\W*(no[ _]text|no readable text|none|\[illegible\])\W*", t or "none"):
        return ""
    return t.replace("\\n", "\n").strip()


def _parse_answer(content):
    try:
        return json.loads(content)
    except ValueError:
        a, b = content.find("{"), content.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(content[a:b + 1])
            except ValueError:
                pass
        raise ModelError("the local model gave an answer I could not read")


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}


def partial_json(text):
    """Read JSON that is still being written, as far as it goes.

    '{"headline": "Priya wants to le' gives {"headline": "Priya wants to le"}.
    Unfinished strings come back as far as they are written; an unfinished
    number or key is left out until it is complete.
    """
    n = len(text)

    def ws(i):
        while i < n and text[i] in " \t\r\n":
            i += 1
        return i

    def string(i):                               # text[i] is the opening quote
        out, i = [], i + 1
        while i < n:
            c = text[i]
            if c == '"':
                return "".join(out), i + 1, True
            if c == "\\":
                if i + 1 >= n:
                    break
                e = text[i + 1]
                if e == "u":
                    if i + 6 > n:
                        break
                    try:
                        out.append(chr(int(text[i + 2:i + 6], 16)))
                    except ValueError:
                        pass
                    i += 6
                    continue
                out.append(_ESCAPES.get(e, e))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out), n, False

    def value(i, depth=0):
        i = ws(i)
        if i >= n or depth > 20:
            return None, i, False
        c = text[i]
        if c == "{":
            obj, i = {}, i + 1
            while True:
                i = ws(i)
                if i >= n:
                    return obj, i, False
                if text[i] == "}":
                    return obj, i + 1, True
                if text[i] == ",":
                    i += 1
                    continue
                if text[i] != '"':
                    return obj, i, False
                key, i, ok = string(i)
                i = ws(i)
                if not ok or i >= n or text[i] != ":":
                    return obj, i, False
                v, i, ok = value(i + 1, depth + 1)
                if v is not None:
                    obj[key] = v
                if not ok:
                    return obj, i, False
        if c == "[":
            arr, i = [], i + 1
            while True:
                i = ws(i)
                if i >= n:
                    return arr, i, False
                if text[i] == "]":
                    return arr, i + 1, True
                if text[i] == ",":
                    i += 1
                    continue
                v, i, ok = value(i, depth + 1)
                if v is not None:
                    arr.append(v)
                if not ok:
                    return arr, i, False
        if c == '"':
            return string(i)
        j = i
        while j < n and text[j] not in ",}] \t\r\n":
            j += 1
        if j >= n:
            return None, j, False                # a number may still be growing
        try:
            return json.loads(text[i:j]), j, True
        except ValueError:
            return None, j, True

    try:
        return value(0)[0]
    except RecursionError:
        return None


TRANSCRIBE_SYSTEM = (
    "You transcribe photos of handwritten notes, whiteboards and screenshots for their owner. "
    "Copy the text exactly as written, keeping line breaks. Do not correct, tidy, summarise, translate or add anything, "
    "and do not add any introduction or comment of your own. "
    "Where a word is hard to read, give your best guess followed by [?]. "
    "If you cannot read a word at all write [illegible]. If the image has no readable text, reply only: NO TEXT")
TRANSCRIBE_ASK = "What text is in this image? Copy it exactly."
TRANSCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["handwriting", "whiteboard", "screenshot", "printed", "no_text"]},
        "text": {"type": "string"},
    },
    "required": ["kind", "text"],
}
POINTS = {"type": "array", "items": {
    "type": "object",
    "properties": {"point": {"type": "string"}, "sources": {"type": "array", "items": {"type": "integer"}}},
    "required": ["point", "sources"]}}
PREP_SCHEMA = {
    "type": "object",
    "properties": {"headline": {"type": "string"}, "talking_points": POINTS, "watch_outs": POINTS,
                   "gaps": {"type": "array", "items": {"type": "string"}}},
    "required": ["headline", "talking_points", "watch_outs", "gaps"],
}
ASK_SCHEMA = {
    "type": "object",
    "properties": {"found": {"type": "boolean"}, "answer": POINTS, "caveat": {"type": "string"}},
    "required": ["found", "answer", "caveat"],
}
RULES = (
    "Rules:\n"
    "1. Use ONLY the numbered excerpts. They are the manager's own notes. Never add outside knowledge and never guess.\n"
    "2. Every point must list the numbers of the excerpts it comes from in `sources`.\n"
    "3. Notes are messy and some are out of date. When excerpts disagree, the most recently dated one is probably "
    "current: say what changed and give both dates.\n"
    "4. Excerpts marked as handwriting may contain misread words. Treat [?] words as uncertain.\n"
    "5. Plain English, one short sentence per point, at most 22 words. Copy names, numbers and dates exactly.\n")
PREP_SYSTEM = (
    "You help a busy manager get ready for a meeting using excerpts from their own scattered notes.\n" + RULES +
    "6. headline: one sentence on where things stand. talking_points: up to 3 things to raise, most important first. "
    "watch_outs: up to 2 things that changed, conflict, look stale or are overdue. "
    "gaps: up to 2 things the manager would want to know for this meeting that the excerpts do NOT answer.")
ASK_SYSTEM = (
    "You answer a manager's question using excerpts from their own scattered notes.\n" + RULES +
    "6. If the excerpts do not answer the question, set found to false, leave answer empty, and say in caveat what "
    "is missing. A related excerpt that does not actually answer the question does not count.\n"
    "7. If an excerpt answers the question, set found to true and answer it, even if the wording differs. Only if "
    "the excerpts contradict what the question assumes (for example it asks about a pilot with a vendor the notes "
    "say was ruled out), set found to false and use answer for up to 2 points on what the excerpts DO say.\n"
    "8. answer: up to 2 points. caveat: one short sentence about uncertainty, or an empty string.")


# Shorter instructions for small models (under 3 billion parameters), which follow a few
# plain rules better than a long list. The app's own checks are the same either way.
PREP_SYSTEM_SHORT = (
    "You help a manager get ready for a meeting. Use ONLY the numbered note excerpts. Never guess.\n"
    "Each point: one short sentence, copied names, numbers and dates, and the excerpt numbers it comes from in sources.\n"
    "If two excerpts disagree, the one with the later date is right.\n"
    "headline: where things stand, in one sentence. talking_points: up to 3. watch_outs: up to 2 things that changed "
    "or are overdue. gaps: up to 2 things the notes do not say.")
ASK_SYSTEM_SHORT = (
    "You answer a manager's question from their own notes. Use ONLY the numbered note excerpts. Never guess.\n"
    "If an excerpt answers it, set found to true and give up to 2 short points, each with the excerpt numbers in sources.\n"
    "If the excerpt with the later date disagrees with an earlier one, the later one is right.\n"
    "If no excerpt answers it, set found to false and leave answer empty.")


def small(model_status, model):
    """Is this a small model (under 3 billion parameters)?"""
    return (model_status or {}).get(model, 99) < 3
