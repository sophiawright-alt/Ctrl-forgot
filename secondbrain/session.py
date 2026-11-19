"""One session: what the assistant may see right now, and what it holds in memory.

Nothing here is ever written down. When a folder is switched off its notes
are dropped from memory at once; when the session ends everything is dropped.
Even the permissions are forgotten, so each session starts blind.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import queue
import threading
import time

from . import dates, extract, llm
from .index import Index, chunk_doc

TEXT_TYPES = {".txt", ".md", ".markdown", ".text", ".csv", ".log", ".org", ".url", ".docx"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
PDF_TYPES = {".pdf"}
MAX_TEXT, MAX_IMAGE = 5_000_000, 20_000_000
LOOSE = "(loose files)"


class Doc:
    def __init__(self, rel, unit, path, kind, stat):
        self.id = rel.replace(os.sep, "/")
        self.unit = unit
        self.path = path
        self.kind = kind                      # "text" or "image"
        self.title = os.path.splitext(os.path.basename(rel))[0].replace("_", " ").replace("-", " ")
        self.size, self.mtime = stat.st_size, stat.st_mtime
        self.sha256 = None
        self.lines = []
        self.date, self.date_source = dt.date.fromtimestamp(stat.st_mtime), "file's last-modified time"
        self.status = "ready"                 # ready | queued | reading | no_model | failed | empty
        self.image_kind = None
        self.uncertain = False
        self.note = ""
        self.images = None                    # page pictures of a scanned PDF (memory only)

    def set_text(self, text):
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.date, self.date_source = dates.note_date(os.path.basename(self.id), self.lines, self.mtime)

    def public(self):
        return {"id": self.id, "name": self.id.split("/", 1)[-1] if self.unit != LOOSE else self.id,
                "kind": self.kind, "status": self.status, "date": self.date.isoformat(),
                "image_kind": self.image_kind, "uncertain": self.uncertain, "note": self.note}


class Session:
    def __init__(self, root, ledger, model, today=None):
        self.root = os.path.normcase(os.path.realpath(root))
        self.ledger = ledger
        self.model = model
        self._today = today
        self.lock = threading.RLock()
        self.allowed = set()                  # unit names switched on
        self.hidden = set()                   # individual note ids switched off
        self.docs = {}                        # id -> Doc
        self.skipped = {}                     # unit -> count of unsupported files
        self.index = Index()
        self.vectors = {}                     # chunk id -> embedding (memory only)
        self.started = time.time()
        self._last_sync = 0.0
        self._jobs = queue.Queue()
        self._foreground = 0                  # answers being written right now
        self.reading_now = None               # the image being read, if any
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    @property
    def today(self):
        return self._today or dt.date.today()

    # ---- permissions (also consulted by the lock on every file open) -----
    def _unit_of(self, real_path):
        rel = os.path.relpath(real_path, self.root)
        parts = rel.split(os.sep)
        if parts[0] in ("..", "."):
            return None, rel
        if len(parts) == 1 and not os.path.isdir(real_path):
            return LOOSE, rel
        return parts[0], rel

    def is_allowed(self, real_path):
        unit, rel = self._unit_of(real_path)
        return unit in self.allowed and rel.replace(os.sep, "/") not in self.hidden

    def units(self):
        """Top-level folders in the notes folder. Looks at names only, not inside."""
        names, loose = [], False
        try:
            with os.scandir(self.root) as it:
                for e in it:
                    if e.name.startswith(".") or e.is_symlink():
                        continue
                    if e.is_dir():
                        names.append(e.name)
                    elif os.path.splitext(e.name)[1].lower() in TEXT_TYPES | IMAGE_TYPES | PDF_TYPES:
                        loose = True
        except OSError:
            pass
        names.sort(key=str.lower)
        return names + ([LOOSE] if loose else [])

    def set_allowed(self, unit, on):
        with self.lock:
            if unit not in self.units():
                raise ValueError("no such folder")
            if on:
                self.allowed.add(unit)
                self.ledger.record("allow", {"folder": unit})
                # Get the model into memory now, so the first answer does not wait for it.
                threading.Thread(target=self.model.warm, daemon=True).start()
            else:
                self.allowed.discard(unit)
                gone = [d for d in self.docs.values() if d.unit == unit]
                for d in gone:
                    del self.docs[d.id]
                self.hidden -= {d.id for d in gone}
                self.ledger.record("revoke", {"folder": unit, "notes_forgotten": len(gone)})
            self.sync(force=True, rebuild=True)

    def set_hidden(self, doc_id, hide):
        with self.lock:
            if hide:
                if doc_id in self.docs:
                    del self.docs[doc_id]
                self.hidden.add(doc_id)
                self.ledger.record("revoke", {"note": doc_id, "notes_forgotten": 1})
            else:
                self.hidden.discard(doc_id)
                self.ledger.record("allow", {"note": doc_id})
            self.sync(force=True, rebuild=True)

    # ---- loading --------------------------------------------------------
    def _walk(self, unit):
        """Yield (rel, path, stat) for supported files in an allowed unit."""
        skipped = 0
        if unit == LOOSE:
            stack, recurse = [self.root], False
        else:
            stack, recurse = [os.path.join(self.root, unit)], True
        while stack:
            folder = stack.pop()
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                if e.name.startswith((".", "~$")) or e.is_symlink():   # "~$" = Word's temporary lock file
                    continue
                if e.is_dir():
                    if recurse:
                        stack.append(e.path)
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                if ext not in TEXT_TYPES | IMAGE_TYPES | PDF_TYPES:
                    skipped += 1
                    continue
                yield os.path.relpath(e.path, self.root), e.path, e.stat()
        self.skipped[unit] = skipped

    def _load(self, rel, path, unit, stat):
        ext = os.path.splitext(path)[1].lower()
        kind = "image" if ext in IMAGE_TYPES else "text"
        doc = Doc(rel, unit, path, kind, stat)
        if stat.st_size > (MAX_IMAGE if (kind == "image" or ext in PDF_TYPES) else MAX_TEXT):
            doc.status, doc.note = "failed", "too large to read"
            return doc
        try:
            with open(path, "rb") as f:           # the lock records this read
                data = f.read()
        except OSError as e:
            doc.status, doc.note = "failed", "could not be opened: %s" % e
            return doc
        doc.sha256 = hashlib.sha256(data).hexdigest()
        if ext in PDF_TYPES:
            from . import pdftext
            try:
                text, pages = pdftext.extract(data)
            except pdftext.PDFError as e:
                doc.status, doc.note = "failed", _pdf_problem(e)
                return doc
            except Exception as e:                   # a damaged or unusual file
                doc.status, doc.note = "failed", "PDF could not be read (%s)" % str(e)[:80]
                return doc
            if text.strip():
                doc.set_text(text)
            elif pages:                              # a scan: read the pictures like a photo
                doc.kind, doc.images, doc.status = "image", pages, "queued"
                self._jobs.put(("transcribe", doc.id, doc.sha256))
            else:
                doc.status, doc.note = "empty", "no text in this PDF, and no page pictures I can read"
            return doc
        if kind == "text":
            if ext == ".docx":
                try:
                    text = docx_text(data)
                except Exception as e:
                    doc.status, doc.note = "failed", _docx_problem(data, e)
                    return doc
            else:
                text = data.decode("utf-8", "replace")
            doc.set_text(text)
            if not any(l.strip() for l in doc.lines):
                doc.status = "empty"
        else:
            doc.status = "queued"
            self._jobs.put(("transcribe", doc.id, doc.sha256))
        return doc

    def sync(self, force=False, rebuild=False):
        """Bring memory in line with the allowed folders (new, edited, deleted notes)."""
        with self.lock:
            if not force and time.time() - self._last_sync < 3:
                return
            self._last_sync = time.time()
            changed = rebuild
            seen = set()
            for unit in list(self.allowed):
                if unit not in self.units():
                    self.allowed.discard(unit)
                    continue
                for rel, path, stat in self._walk(unit):
                    doc_id = rel.replace(os.sep, "/")
                    if doc_id in self.hidden:
                        continue
                    seen.add(doc_id)
                    old = self.docs.get(doc_id)
                    if old and old.mtime == stat.st_mtime and old.size == stat.st_size:
                        continue
                    self.docs[doc_id] = self._load(rel, path, unit, stat)
                    changed = True
            for doc_id in [d for d in self.docs if d not in seen]:
                del self.docs[doc_id]
                changed = True
            status = self.model.refresh()
            if status.get("vision"):
                for d in self.docs.values():
                    if d.kind == "image" and d.status == "no_model":
                        d.status = "queued"
                        self._jobs.put(("transcribe", d.id, d.sha256))
            if changed:
                self._rebuild()

    def _rebuild(self):
        chunks = []
        for d in sorted(self.docs.values(), key=lambda d: d.id):
            if d.status == "ready" and d.lines:
                chunks.extend(chunk_doc(d))
        self.index = Index(chunks)
        self.vectors = {k: v for k, v in self.vectors.items() if k in self.index.by_id}
        self._jobs.put(("embed", None, None))

    # ---- background work: reading images, meaning vectors ----------------
    def _work(self):
        while True:
            job, doc_id, sha = self._jobs.get()
            try:
                if job == "transcribe":
                    self._transcribe(doc_id, sha)
                elif job == "embed":
                    self._embed()
            except Exception as e:               # never let the worker die
                self.ledger.record("note", {"message": "background task failed: %s" % e})

    def _transcribe(self, doc_id, sha):
        with self.lock:
            doc = self.docs.get(doc_id)
            if not doc or doc.sha256 != sha:
                return                           # switched off or changed meanwhile
            vision = self.model.refresh().get("vision")
            if not vision:
                doc.status, doc.note = "no_model", "no local model that can read images is installed"
                return
            doc.status = "reading"
            path, pages = doc.path, doc.images
        try:
            if pages is None:
                with open(path, "rb") as f:
                    pages = [f.read()]
            texts, kinds = [], []
            for n, img in enumerate(pages, 1):
                self._yield_to_foreground()
                self.reading_now = doc_id
                try:
                    out = self.model.chat_json(vision, llm.TRANSCRIBE_SYSTEM,
                                           "Transcribe this image.", llm.TRANSCRIBE_SCHEMA, images=[img], timeout=900,
                                           purpose="read image %s%s" % (doc_id, " page %d" % n if len(pages) > 1 else ""))
                finally:
                    self.reading_now = None
                t = str(out.get("text") or "").replace("\\n", "\n").strip()
                if t and t.lower() not in ("no text", "no_text", "[illegible]", "none"):   # text beats the label
                    texts.append(("[page %d]\n" % n if len(pages) > 1 else "") + t)
                    kind = out.get("kind")
                    kinds.append(kind if kind and kind != "no_text" else "handwriting")
                with self.lock:
                    if self.docs.get(doc_id) is not doc:
                        return                   # switched off while reading: stop and discard
        except Exception as e:
            with self.lock:
                if self.docs.get(doc_id) is doc:
                    doc.tries = getattr(doc, "tries", 0) + 1
                    if "timed out" in str(e) and doc.tries < 2:
                        doc.status, doc.note = "queued", "taking a long time; trying once more"
                        self._jobs.put(("transcribe", doc_id, sha))      # back of the queue
                    else:
                        doc.status, doc.note = "failed", str(e)[:200] + (
                            ". Large phone photos are slow to read: try saving a smaller copy "
                            "(about 1500 pixels wide)" if "timed out" in str(e) else "")
            return
        with self.lock:
            if self.docs.get(doc_id) is not doc:
                return                           # permission was withdrawn while reading: discard
            text = "\n\n".join(texts)
            out = {"kind": kinds[0] if kinds else "no_text"}
            doc.image_kind = out.get("kind") or "handwriting"
            if not text or doc.image_kind == "no_text":
                doc.status, doc.note = "empty", "no readable text found"
            else:
                doc.set_text(text)
                doc.uncertain = "[?]" in text or "[illegible]" in text
                doc.status = "ready"
            self._rebuild()

    def _embed(self):
        self._yield_to_foreground()
        em = self.model.refresh().get("embed")
        if not em:
            return
        with self.lock:
            todo = [c for c in self.index.chunks if c.id not in self.vectors][:64]
        if not todo:
            return
        try:
            vecs = self.model.embed(em, [c.doc.title + "\n" + c.text for c in todo])
        except llm.ModelError:
            return
        with self.lock:
            for c, v in zip(todo, vecs):
                if c.id in self.index.by_id:
                    self.vectors[c.id] = _unit(v)
            if any(c.id not in self.vectors for c in self.index.chunks):
                self._jobs.put(("embed", None, None))

    # ---- views ----------------------------------------------------------
    # ---- answers you are waiting for come before background reading ------
    def foreground_begin(self):
        with self.lock:
            self._foreground += 1

    def foreground_end(self):
        with self.lock:
            self._foreground = max(0, self._foreground - 1)

    def _yield_to_foreground(self, limit=600):
        t0 = time.time()
        while self._foreground and time.time() - t0 < limit:
            time.sleep(0.2)

    def busy(self):
        with self.lock:
            return sum(1 for d in self.docs.values() if d.status in ("queued", "reading"))

    def state(self):
        with self.lock:
            units = []
            for name in self.units():
                on = name in self.allowed
                docs = sorted((d for d in self.docs.values() if d.unit == name), key=lambda d: d.id)
                hidden = sorted(h for h in self.hidden if (h.split("/")[0] == name) or (name == LOOSE and "/" not in h))
                units.append({"name": name, "allowed": on, "notes": [d.public() for d in docs],
                              "hidden": hidden, "skipped": self.skipped.get(name, 0) if on else 0})
            ready = [d for d in self.docs.values() if d.status == "ready"]
            people = extract.find_people(ready)
            return {
                "folder": self.root,
                "today": self.today.isoformat(),
                "units": units,
                "notes_in_memory": len(self.docs),
                "chunks_in_memory": len(self.index.chunks),
                "meaning_search": bool(self.vectors),
                "reading": self.busy(),
                "people": people,
                "topics": extract.find_topics(ready, people),
            }

    # ---- ending ---------------------------------------------------------
    def end(self):
        """Check the notes are untouched, then forget everything."""
        with self.lock:
            checked, changed, missing = 0, [], []
            for d in sorted(self.docs.values(), key=lambda d: d.id):
                if not d.sha256:
                    continue
                try:
                    with open(d.path, "rb") as f:
                        now = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    missing.append(d.id)
                    continue
                checked += 1
                if now != d.sha256:
                    changed.append(d.id)
            receipt = self.ledger.snapshot(self.root, limit=400)
            receipt.update({
                "notes_checked": checked, "notes_changed": changed, "notes_missing": missing,
                "forgotten": {"notes": len(self.docs), "search_entries": len(self.index.chunks),
                              "meaning_vectors": len(self.vectors), "permissions": len(self.allowed)},
                "minutes": round((time.time() - self.started) / 60, 1),
            })
            self.allowed.clear()
            self.hidden.clear()
            self.docs.clear()
            self.skipped.clear()
            self.index = Index()
            self.vectors = {}
            while not self._jobs.empty():
                try:
                    self._jobs.get_nowait()
                except queue.Empty:
                    break
        receipt["models_unloaded"] = self.model.unload()
        self.ledger.reset()
        self.started = time.time()
        return receipt


def _pdf_problem(error):
    if str(error) == "encrypted":
        return ("locked PDF (a password, or copy/print restrictions). Open it and use Print, then "
                "'Microsoft Print to PDF', to make an unlocked copy")
    if str(error) == "not a PDF":
        return "not really a PDF, despite the name"
    return "PDF could not be read (%s)" % error


def _docx_problem(data, error):
    """A plain reason a .docx could not be read, so the user knows what to do."""
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ("protected Word file (a password or a sensitivity label such as 'Confidential'). "
                "Remove the protection or copy the text into a new document")
    if data[:2] != b"PK":
        return "not really a .docx (probably an old .doc renamed). Open it in Word and Save As .docx"
    return "Word file could not be read (%s)" % str(error)[:80]


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def docx_text(data):
    """Pull the text out of a Word document, one paragraph per line.

    A .docx is a zip of XML, so this needs nothing but Python itself, and it
    reads from the bytes already in memory: nothing is unpacked to disk.
    Tags are matched by name only, so both the normal and the "Strict"
    Word formats work.
    """
    import io
    import zipfile
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        main = "word/document.xml" if "word/document.xml" in names else next(
            (n for n in names if n.startswith("word/document") and n.endswith(".xml")), None)
        if not main:
            raise ValueError("no document body inside")
        root = ET.fromstring(z.read(main))
    lines = []
    for para in root.iter():
        if _local(para.tag) != "p":
            continue
        parts, style, listed = [], "", False
        for node in para.iter():
            name = _local(node.tag)
            if name == "t":
                parts.append(node.text or "")
            elif name == "tab":
                parts.append("\t")
            elif name in ("br", "cr"):
                parts.append("\n")
            elif name == "pStyle":
                style = next((v for k, v in node.attrib.items() if _local(k) == "val"), "")
            elif name == "numPr":
                listed = True
        text = "".join(parts)
        if text.strip() and style.lower().startswith(("heading", "title")):
            text = "# " + text                       # keep headings recognisable
        elif text.strip() and listed:
            text = "- " + text                       # bulleted or numbered list item
        lines.extend(text.split("\n"))
    return "\n".join(lines)


def _unit(v):
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]
