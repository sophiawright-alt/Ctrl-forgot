"""Run with:  python3 -B tests/test_all.py

Starts the real app against the sample notes and a stand-in local model, then
checks the behaviour the brief asks for. No packages needed.
"""
import datetime as dt
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

from secondbrain import dates, extract            # noqa: E402
import mock_ollama                                # noqa: E402

PORT = 8791


def tree(root):
    out = {}
    for folder, _, files in os.walk(root):
        for name in files:
            p = os.path.join(folder, name)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


def call(path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    if body is None:
        c.request("GET", path)
    else:
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "X-Second-Brain": "1"})
    r = c.getresponse()
    data = json.loads(r.read() or b"{}")
    c.close()
    return r.status, data


class Rules(unittest.TestCase):
    def test_dates(self):
        ref = dt.date(2026, 10, 20)
        self.assertEqual(dates.find_dates("by Fri", ref)[0]["date"], dt.date(2026, 10, 23))
        self.assertEqual(dates.find_dates("28/10/2026 catch up", ref)[0]["date"], dt.date(2026, 10, 28))
        self.assertEqual(dates.find_dates("1:1 and 24/7 and 1/2 day", ref), [])

    def test_actions(self):
        ref = dt.date(2026, 10, 6)
        a = extract.classify_line("Priya to send revised forecast by Fri", ref)
        self.assertEqual((a["owner"], a["done"], a["due"]), ("Priya", False, dt.date(2026, 10, 9)))
        self.assertTrue(extract.classify_line("- [x] book the room", ref)["done"])
        self.assertIsNone(extract.classify_line("just some chatter about lunch", ref))

    def test_changes(self):
        found = extract.find_changes([("a", "Helix budget £40k for year one", dt.date(2026, 10, 6)),
                                      ("b", "Helix budget cut to £28k for year one", dt.date(2026, 11, 3)),
                                      ("c", "conference budget ok, £600 for year one", dt.date(2026, 11, 10))])
        self.assertEqual([(f[0], f[1], f[3], f[4]) for f in found], [("a", "b", "£40k", "£28k")])


class Word(unittest.TestCase):
    def test_strict_and_protected(self):
        import io, zipfile
        from secondbrain.session import docx_text, _docx_problem
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:     # "Strict" Word uses a different namespace
            z.writestr("word/document.xml", '<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main">'
                       '<w:body><w:p><w:r><w:t>Tom to send the list by Fri</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(docx_text(buf.getvalue()), "Tom to send the list by Fri")
        self.assertIn("protected", _docx_problem(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", None))


def stream(path, body):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "X-Second-Brain": "1"})
    r = c.getresponse()
    events = [json.loads(l) for l in r.read().decode().splitlines() if l.strip()]
    c.close()
    return events


class Checking(unittest.TestCase):
    def test_invented_points_are_removed(self):
        from secondbrain.brief import _ground
        ev = [{"n": 1, "text": "Helix budget cut to £28k for year one. Approved by finance.", "date_label": "3 Nov",
               "title": "steering prep", "date": "2026-11-03"},
              {"n": 2, "text": "Helix budget £40k agreed.", "date_label": "6 Oct", "title": "vendor review",
               "date": "2026-10-06"}]
        kept, dropped = _ground([
            {"point": "The Helix budget was cut to £28k for year one.", "sources": [1]},       # true
            {"point": "The board approved a further £500k.", "sources": [1]},                  # invented number
            {"point": "The Helix budget was cut from £40k.", "sources": [1]},                  # number from note 2
            {"point": "Everyone is happy.", "sources": []}], ev, 5)                            # no source
        self.assertEqual([(k["text"][:22], k["check"]) for k in kept],
                         [("The Helix budget was c", "ok"), ("The Helix budget was c", "verify")])
        self.assertEqual(dropped, 2)


class Speed(unittest.TestCase):
    """Summaries are written while you wait, so they get every advantage."""

    def setUp(self):
        self.mock = mock_ollama.start(0)
        self.saved = dict(mock_ollama.MODELS)
        from secondbrain.ledger import Ledger
        from secondbrain.llm import LocalModel
        self.ledger = Ledger()
        self.model = LocalModel(self.ledger, port=self.mock.server_address[1])

    def tearDown(self):
        mock_ollama.MODELS.clear()
        mock_ollama.MODELS.update(self.saved)
        self.mock.shutdown()

    def test_quickest_sensible_model_writes_summaries(self):
        mock_ollama.MODELS["qwen3-vl:4b"] = "4.4B"
        mock_ollama.MODELS["qwen3-vl:2b"] = "2.1B"
        st = self.model.refresh(force=True)
        self.assertEqual((st["chat"], st["vision"]), ("qwen3-vl:4b", "qwen3-vl:8b"))
        os.environ["SECOND_BRAIN_MODEL"] = "qwen3-vl:8b"
        try:
            self.assertEqual(self.model.refresh(force=True)["chat"], "qwen3-vl:8b")       # your choice wins
        finally:
            del os.environ["SECOND_BRAIN_MODEL"]

    def test_direct_answering_models_preferred(self):
        mock_ollama.MODELS["qwen3-vl:4b"] = "4.4B"
        mock_ollama.MODELS["qwen3-vl:4b-instruct"] = "4.4B"
        mock_ollama.THINKERS.update({"qwen3-vl:4b", "qwen3-vl:8b"})
        try:
            st = self.model.refresh(force=True)
            self.assertEqual((st["chat"], st["vision"]), ("qwen3-vl:4b-instruct", "qwen3-vl:4b-instruct"))
        finally:
            mock_ollama.THINKERS.clear()

    def run_stream(self, **kw):
        from secondbrain.llm import PREP_SCHEMA
        return list(self.model.stream_json("qwen3-vl:8b", "system", "brief me", PREP_SCHEMA, every=0, **kw))

    def test_thinking_model_still_answers(self):
        mock_ollama.MODE["think"] = "think"
        try:
            got = self.run_stream()
        finally:
            mock_ollama.MODE["think"] = "none"
        self.assertEqual(got[0], ("thinking", True))
        self.assertEqual(got[-1][1]["headline"], "Priya wants to lead the Helix pilot.")
        self.assertGreater(self.ledger.events[-1]["detail"]["thinking_chars"], 0)

    def test_silent_answer_is_retried(self):
        mock_ollama.MODE["think"] = "empty"          # an Ollama version that says nothing with think=false
        try:
            got = self.run_stream()
        finally:
            mock_ollama.MODE["think"] = "none"
        self.assertEqual(got[-1][0], "done")
        self.assertIn("qwen3-vl:8b", self.model._no_think)

    def test_length_cap_keeps_what_was_written(self):
        got = self.run_stream(max_chars=150)
        self.assertEqual(got[-1][0], "done")
        self.assertEqual(got[-1][1]["headline"], "Priya wants to lead the Helix pilot.")

    def test_small_models_get_short_instructions(self):
        from secondbrain import llm
        self.model._sizes = {"qwen3-vl:2b-instruct": 2.1, "qwen3-vl:4b-instruct": 4.4}
        self.assertTrue(llm.small(self.model._sizes, "qwen3-vl:2b-instruct"))
        self.assertFalse(llm.small(self.model._sizes, "qwen3-vl:4b-instruct"))
        self.assertLess(len(llm.ASK_SYSTEM_SHORT), len(llm.ASK_SYSTEM))

    def test_one_model_does_both(self):
        st = self.model.refresh(force=True)
        self.assertEqual((st["chat"], st["vision"]), ("qwen3-vl:8b", "qwen3-vl:8b"))

    def test_photos_get_room_to_be_read(self):
        mock_ollama.MODELS["qwen3-vl:2b-instruct"] = "2.1B"
        mock_ollama.MODELS["qwen3-vl:4b-instruct"] = "4.4B"
        os.environ["SECOND_BRAIN_CONTEXT"] = "3072"
        try:
            st = self.model.refresh(force=True)
            self.assertEqual((st["chat"], st["vision"]), ("qwen3-vl:4b-instruct", "qwen3-vl:8b"))
            self.assertEqual(self.model.context_for(st["chat"]), 3072)               # your setting, for answers
            self.assertEqual(self.model.context_for("qwen3-vl:2b-instruct"), 3072)
            self.assertEqual(self.model.context_for(st["vision"]), 8192)             # photos always get room
            self.assertEqual(self.model.context_for(st["chat"], reading_images=True), 8192)
        finally:
            del os.environ["SECOND_BRAIN_CONTEXT"]

    def read_photo(self, label):
        import shutil
        import tempfile
        from secondbrain.session import Session
        mock_ollama.MODE["label"] = label
        folder = tempfile.mkdtemp()
        try:
            os.mkdir(os.path.join(folder, "photos"))
            shutil.copy(os.path.join(ROOT, "sample_notes", "photos", "2026-11-12 chat with dan.png"),
                        os.path.join(folder, "photos", "chat.png"))
            s = Session(folder, self.ledger, self.model)
            s.set_allowed("photos", True)
            for _ in range(40):
                if s.docs["photos/chat.png"].status not in ("queued", "reading"):
                    break
                time.sleep(0.1)
            return s.docs["photos/chat.png"]
        finally:
            mock_ollama.MODE["label"] = "normal"
            shutil.rmtree(folder)

    def test_photos_are_read_with_a_plain_question(self):
        d = self.read_photo("normal")
        self.assertEqual((d.status, d.image_kind), ("ready", "screenshot"))
        self.assertIn("Dan: I can give you Kofi 2 days a week from 1 Dec.", d.lines)
        chat = mock_ollama.PROMPTS[-1]
        self.assertNotIn("format", [k for k in chat[-1]])                     # no fixed reply format
        img_req = [c for c in mock_ollama.CHATS if c[1].get("num_ctx")][-1]
        self.assertEqual(img_req[1]["num_ctx"], 8192)                           # room for the photo
        reads = [e["detail"] for e in self.ledger.events if e["detail"].get("purpose", "").startswith("read image")]
        self.assertIn("Kofi", reads[-1]["reply"])                               # "What it replied" for photos

    def test_photo_replies_are_tidied(self):
        for label in ("fenced", "json"):
            d = self.read_photo(label)
            self.assertEqual(d.status, "ready", label)
            self.assertFalse(any("```" in l or l.startswith("{") for l in d.lines), label)
        self.assertEqual(self.read_photo("blank").status, "empty")

    def test_same_working_memory_everywhere(self):
        self.model.refresh(force=True)
        self.assertEqual(self.model.context_for("qwen3-vl:4b-instruct"), 4096)
        self.assertEqual(self.model.context_for("qwen3-vl:4b-thinking"), 8192)
        mock_ollama.WARMS.clear()
        self.model._warmed.clear()
        self.model.warm()
        self.assertEqual(mock_ollama.WARMS[-1], 4096)          # preloading asks for the same as answering

    def test_preloading(self):
        mock_ollama.LOADED.clear()
        self.assertFalse(self.model.loaded("qwen3-vl:8b"))
        self.model.warm()
        self.assertTrue(self.model.loaded("qwen3-vl:8b"))
        self.assertIn("No notes were sent", self.ledger.events[-1]["detail"]["message"])

    def test_summaries_go_before_image_reading(self):
        import shutil
        import tempfile
        from secondbrain.session import Session
        folder = tempfile.mkdtemp()
        try:
            os.mkdir(os.path.join(folder, "photos"))
            shutil.copy(os.path.join(ROOT, "sample_notes", "photos", "2026-11-12 chat with dan.png"),
                        os.path.join(folder, "photos", "chat.png"))
            s = Session(folder, self.ledger, self.model)
            s.foreground_begin()                                  # someone is waiting for an answer
            s.set_allowed("photos", True)
            time.sleep(1.0)
            self.assertEqual(s.docs["photos/chat.png"].status, "reading")   # picked up, but paused
            self.assertEqual(s.reading_now, None)
            s.foreground_end()
            for _ in range(40):
                if s.docs["photos/chat.png"].status == "ready":
                    break
                time.sleep(0.1)
            self.assertEqual(s.docs["photos/chat.png"].status, "ready")
        finally:
            shutil.rmtree(folder)


class Streaming(unittest.TestCase):
    def test_partial_json(self):
        from secondbrain.llm import partial_json
        self.assertEqual(partial_json('{"headline": "Priya wants to le'), {"headline": "Priya wants to le"})
        self.assertEqual(partial_json('{"a": [{"point": "x\\n y", "sources": [1, 2'),
                         {"a": [{"point": "x\n y", "sources": [1]}]})
        self.assertEqual(partial_json('{"found": tr'), {})
        self.assertEqual(partial_json(''), None)

    def test_text_arrives_mid_word(self):
        from secondbrain.ledger import Ledger
        from secondbrain.llm import LocalModel, PREP_SCHEMA
        mock = mock_ollama.start(0)
        try:
            ledger = Ledger()
            m = LocalModel(ledger, port=mock.server_address[1])
            got = list(m.stream_json("qwen3-vl:8b", "system", "brief me", PREP_SCHEMA, every=0))
            heads = [o.get("headline", "") for k, o in got if k == "partial"]
            self.assertTrue(any(0 < len(h) < len("Priya wants to lead the Helix pilot.") for h in heads))
            self.assertEqual(got[-1][0], "done")
            self.assertEqual(ledger.model_calls, 1)
            stream_ = m.stream_json("qwen3-vl:8b", "system", "brief me", PREP_SCHEMA, every=0)
            next(stream_)
            stream_.close()                                  # the user asked something else
            self.assertIn("stopped early", ledger.events[-1]["detail"]["error"])
        finally:
            mock.shutdown()


def _pdf(objects, root=1, extra_trailer=""):
    """Build a small PDF by hand, so these tests need no PDF library."""
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for o in offsets:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root %d 0 R %s >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1, root, extra_trailer.encode(), xref)
    return bytes(out)


def _stream(dict_body, data):
    return b"<< " + dict_body + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def text_pdf(lines):
    import zlib
    content = b"BT /F1 12 Tf 14 TL 72 760 Td " + b" ".join(b"(%s) Tj T*" % l.encode("latin-1") for l in lines) + b" ET"
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>",
                 b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                 b"/Resources << /Font << /F1 5 0 R >> >> >>",
                 _stream(b"/Filter /FlateDecode", zlib.compress(content)),
                 b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"])


def scan_pdf(jpeg, w=1000, h=1200):
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>",
                 b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                 b"/Resources << /XObject << /Im0 5 0 R >> >> >>",
                 _stream(b"", b"q 595 0 0 842 0 0 cm /Im0 Do Q"),
                 _stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                         b"/BitsPerComponent 8 /Filter /DCTDecode" % (w, h), jpeg)])


class Versions(unittest.TestCase):
    def test_page_and_app_agree(self):
        import re as _re
        from secondbrain import __version__
        with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as f:
            self.assertEqual(_re.search(r'APP_VERSION = "([^"]+)"', f.read()).group(1), __version__)


class PDF(unittest.TestCase):
    def test_text(self):
        from secondbrain import pdftext
        text, images = pdftext.extract(text_pdf(["Agreed: Zebra budget \xa312k", "Sam to send the plan by Fri"]))
        self.assertEqual(text, "Agreed: Zebra budget \u00a312k\nSam to send the plan by Fri")
        self.assertEqual(images, [])

    def test_scan_gives_page_pictures(self):
        from secondbrain import pdftext
        jpeg = b"\xff\xd8\xff\xe0" + b"\0" * 5000 + b"\xff\xd9"
        text, images = pdftext.extract(scan_pdf(jpeg))
        self.assertEqual((text, images), ("", [jpeg]))

    def test_locked(self):
        from secondbrain import pdftext, session
        locked = _pdf([b"<< /Type /Catalog >>", b"<< /Filter /Standard >>"], extra_trailer="/Encrypt 2 0 R")
        with self.assertRaises(pdftext.PDFError) as e:
            pdftext.extract(locked)
        self.assertIn("locked PDF", session._pdf_problem(e.exception))

    def test_scanned_pdf_is_read_by_the_vision_model(self):
        import shutil
        import tempfile
        from secondbrain.ledger import Ledger
        from secondbrain.llm import LocalModel
        from secondbrain.session import Session
        mock = mock_ollama.start(0)
        folder = tempfile.mkdtemp()
        try:
            os.mkdir(os.path.join(folder, "scans"))
            with open(os.path.join(ROOT, "sample_notes", "photos", "2026-11-10 notebook page.jpg"), "rb") as f:
                jpeg = f.read()
            with open(os.path.join(folder, "scans", "2026-11-10 scanned notebook.pdf"), "wb") as f:
                f.write(scan_pdf(jpeg))
            s = Session(folder, Ledger(), LocalModel(Ledger(), port=mock.server_address[1]))
            s.set_allowed("scans", True)
            for _ in range(40):
                d = s.docs["scans/2026-11-10 scanned notebook.pdf"]
                if d.status == "ready":
                    break
                time.sleep(0.1)
            self.assertEqual((d.status, d.kind), ("ready", "image"))
            self.assertIn("P wants to LEAD the Helix pilot", d.lines)
        finally:
            mock.shutdown()
            shutil.rmtree(folder)


class Desktop(unittest.TestCase):
    """The double-click app: closes when its window closes, survives a reload, opens only once."""

    def start(self, port, *extra):
        env = dict(os.environ, SECOND_BRAIN_NO_WINDOW="1", SECOND_BRAIN_BYE_GRACE="0.8",
                   SECOND_BRAIN_IDLE_LIMIT="30", SECOND_BRAIN_FIRST_VISIT="2.5")
        p = subprocess.Popen([sys.executable, "-B", os.path.join(ROOT, "run.py"), "--app", "--port", str(port)] + list(extra),
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                self.req(port, "GET", "/api/ping")
                return p
            except OSError:
                time.sleep(0.1)
        return p

    def req(self, port, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, b"" if method == "POST" else None, headers or {})
        status = c.getresponse().status
        c.close()
        return status

    def test_older_copy_is_not_reused(self):
        from secondbrain import desktop
        mock = mock_ollama.start(0)                          # something answering /api/ping without our version
        try:
            self.assertIsNone(desktop.already_running(mock.server_address[1]))
        finally:
            mock.shutdown()

    def test_window_lifecycle(self):
        port = 8811
        app = self.start(port)
        try:
            bye = {"Origin": "http://127.0.0.1:%d" % port}
            self.assertEqual(self.req(port, "GET", "/api/state"), 200)
            self.assertEqual(self.req(port, "POST", "/api/bye", {"Origin": "http://evil.example"}), 403)
            self.assertEqual(self.req(port, "POST", "/api/bye", bye), 204)          # a reload says goodbye...
            time.sleep(0.2)
            self.req(port, "GET", "/api/state")                                       # ...and comes straight back
            time.sleep(1.5)
            self.assertIsNone(app.poll())                                             # so the app is still running
            second = self.start(port)                                                 # double-clicked again
            self.assertEqual(second.wait(5), 0)                                       # it just shows the window
            self.assertIsNone(app.poll())
            self.req(port, "POST", "/api/bye", bye)                                   # window really closed
            self.assertEqual(app.wait(5), 0)
            self.assertIn(b"Session ended", app.stdout.read())
        finally:
            if app.poll() is None:
                app.kill()

    def test_quits_if_window_never_opens(self):
        app = self.start(8812)
        try:
            self.assertEqual(app.wait(8), 0)
        finally:
            if app.poll() is None:
                app.kill()

    def test_settings_file(self):
        from secondbrain.desktop import read_settings
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("# a comment\nnotes = C:\\Users\\me\\my notes\n# today = 2026-01-01\nmodel=gemma3:4b\n")
        try:
            self.assertEqual(read_settings(f.name), {"notes": "C:\\Users\\me\\my notes", "model": "gemma3:4b"})
        finally:
            os.remove(f.name)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = tree(ROOT)
        cls.mock = mock_ollama.start(0)
        cls.app = subprocess.Popen([sys.executable, "-B", os.path.join(ROOT, "run.py"), "--port", str(PORT),
                                    "--ollama-port", str(cls.mock.server_address[1]), "--today", "2026-11-16"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                call("/api/state")
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.app.terminate()
        cls.app.wait(5)
        cls.mock.shutdown()

    def test_1_starts_blind(self):
        _, s = call("/api/state")
        self.assertTrue(s["locked"])
        self.assertEqual((s["notes_in_memory"], s["receipt"]["files_read"]), (0, 0))
        self.assertEqual(call("/api/ask", {"question": "Helix budget"})[1]["verdict"], "none")
        self.assertNotIn("gpt-oss:120b-cloud", s["model"]["models"])       # cloud models are never used

    def test_2_only_reads_what_is_switched_on(self):
        for f in ("1-1s", "meetings", "scratch", "photos"):
            call("/api/allow", {"folder": f, "on": True})
        for _ in range(40):
            _, s = call("/api/state")
            if not s["reading"] and s["notes_in_memory"] == 13:
                break
            time.sleep(0.25)
        self.assertEqual(s["notes_in_memory"], 13)
        self.assertFalse(any(p.startswith("private-hr") for p in s["receipt"]["files"]))
        self.assertEqual({n["image_kind"] for u in s["units"] for n in u["notes"] if n["kind"] == "image"},
                         {"handwriting", "screenshot"})

    def test_3_requests_must_come_from_the_page(self):
        c = http.client.HTTPConnection("127.0.0.1", PORT)
        c.request("POST", "/api/allow_all", json.dumps({"on": True}), {"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 403)

    def test_4_prep_connects_notes(self):
        _, b = call("/api/prep", {"query": "Priya"})
        done = {a["text"]: a["resolved_by"]["text"] for a in b["probably_done"]}
        self.assertIn("forecast sent", done["Priya to send revised forecast by Fri"])
        self.assertTrue(any(a.get("overdue") for a in b["open_actions"]))
        self.assertTrue(any(d["handwriting"] and "yes in principle" in d["text"] for d in b["decisions"]))
        self.assertTrue(any("private-hr" in h for h in b["honesty"]))
        _, ai = call("/api/prep/ai", {"query": "Priya"})
        self.assertEqual((len(ai["talking_points"]), ai["dropped"]), (1, 2))  # invented + unsourced points removed

    def test_4b_written_answers_stream(self):
        ev = stream("/api/prep/ai/stream", {"query": "Priya"})
        kinds = [e["type"] for e in ev]
        self.assertEqual((kinds[0], kinds[1], kinds[-1]), ("start", "status", "final"))
        model, options, chars = mock_ollama.CHATS[-1]
        self.assertEqual((options.get("num_ctx"), options.get("num_predict")), (4096, None))  # no cap that thinking could eat
        self.assertLess(chars, 7500)                                                         # a short request
        self.assertGreaterEqual(kinds.count("partial"), 2)
        sizes = [len(json.dumps(e["draft"])) for e in ev if e["type"] == "partial"]
        self.assertLess(sizes[0], sizes[-1])                                                # the draft grows
        final = ev[-1]
        self.assertEqual((len(final["talking_points"]), final["dropped"]), (1, 2))        # checked as before
        ev = stream("/api/ask/ai/stream", {"question": "What did we decide about the Helix budget?"})
        self.assertTrue(ev[-1]["found"])
        self.assertEqual(len(ev[-1]["answer"]), 1)                                          # invented point removed
        ev = stream("/api/ask/ai/stream", {"question": "What is Priya's salary?"})
        self.assertEqual([e["type"] for e in ev], ["final"])                                # model never asked

    def test_4c_asking_again_stops_the_old_answer(self):
        mock_ollama.DELAY = 0.05
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
            c.request("POST", "/api/prep/ai/stream", json.dumps({"query": "Tom"}),
                      {"Content-Type": "application/json", "X-Second-Brain": "1"})
            r = c.getresponse()
            self.assertEqual(json.loads(r.readline())["type"], "start")
            c.close()                                        # the page moved on
            for _ in range(60):
                _, st = call("/api/state")
                last = [e for e in st["receipt"]["events"] if e["kind"] == "model"][-1]["detail"]
                if "Tom" in last.get("purpose", ""):
                    break
                time.sleep(0.1)
            self.assertIn("stopped early", last.get("error", ""))
        finally:
            mock_ollama.DELAY = 0.004

    def test_5_find_shows_what_changed(self):
        _, a = call("/api/ask", {"question": "What did we decide about the Helix budget?"})
        self.assertEqual(a["verdict"], "found")
        self.assertEqual([(c["old_value"], c["new_value"]) for c in a["changes"]], [("£40k", "£28k")])
        _, a = call("/api/ask", {"question": "helx budgit"})                 # typos
        self.assertEqual(a["verdict"], "found")

    def test_5b_reads_word_documents(self):
        _, a = call("/api/ask", {"question": "pilot user list"})
        self.assertTrue(any(x["doc"].endswith(".docx") and "user list" in x["text"] for x in a["snippets"]))

    def test_5c_reads_pdfs(self):
        _, a = call("/api/ask", {"question": "which cost centre for Helix?"})
        self.assertTrue(any(x["doc"].endswith(".pdf") and "CC-4471" in x["text"] for x in a["snippets"]))
        _, b = call("/api/prep", {"query": "Mei"})
        self.assertTrue(any("budget letter" in x["text"] and x["resolved_by"]["doc"].endswith(".pdf")
                            for x in b["probably_done"]))

    def test_5e_small_model_habits_do_not_lose_the_answer(self):
        mock_ollama.MODE["ask"] = "unsure"                   # says found: false, writes sources as text
        try:
            _, ai = call("/api/ask/ai", {"question": "When does the Helix pilot start?"})
        finally:
            mock_ollama.MODE["ask"] = "normal"
        self.assertTrue(ai["found"])
        self.assertEqual(ai["dropped"], 0)
        self.assertIn("1 Dec", ai["answer"][0]["text"])
        _, st = call("/api/state")
        reply = [e for e in st["receipt"]["events"] if e["kind"] == "model"][-1]["detail"]["reply"]
        self.assertIn('"found"', reply)                      # what the model said is visible in the receipt
        mock_ollama.MODE["ask"] = "sloppy"                   # other labels, plain sentences, no source numbers
        try:
            _, ai = call("/api/ask/ai", {"question": "When does the Helix pilot start?"})
        finally:
            mock_ollama.MODE["ask"] = "normal"
        self.assertTrue(ai["found"])
        self.assertEqual(len(ai["answer"]), 2)
        self.assertTrue(all(p["sources"] for p in ai["answer"]))   # the app found the notes behind each point

    def test_5f_a_bare_name_becomes_a_question(self):
        call("/api/ask/ai", {"question": "Rina"})
        self.assertIn("Question: What do the notes say about Rina?", mock_ollama.PROMPTS[-1][-1]["content"])
        mock_ollama.MODE["ask"] = "refuse"                   # the model says "not in the notes"
        try:
            _, ai = call("/api/ask/ai", {"question": "When does the Helix pilot start?"})
        finally:
            mock_ollama.MODE["ask"] = "normal"
        self.assertEqual((ai["found"], ai["notes_match"], ai["caveat"]), (False, True, ""))

    def test_5d_questions_about_a_named_thing(self):
        _, a = call("/api/ask", {"question": "When does the Corvid pilot start?"})        # no Corvid pilot
        self.assertEqual((a["verdict"], a["changes"]), ("partial", []))
        self.assertEqual(a["not_together"]["names"], ["Corvid"])
        self.assertTrue(all("Corvid" in x["text"] for x in a["snippets"]))
        self.assertFalse(any("Helix pilot start" in x["text"] for x in a["snippets"]))
        _, a = call("/api/ask", {"question": "when does the corvid pilot start"})          # typed in lower case
        self.assertIn("not_together", a)
        _, a = call("/api/ask", {"question": "which cost centre for Helix?"})              # "Helix" said elsewhere in the letter
        self.assertTrue(any("CC-4471" in x["text"] for x in a["snippets"]))
        _, a = call("/api/ask", {"question": "Why did we rule out Corvid?"})
        self.assertEqual(a["verdict"], "found")
        self.assertIn("audit trail", a["snippets"][0]["text"])

    def test_6_honest_when_it_is_not_there(self):
        _, a = call("/api/ask", {"question": "What is Priya's salary?"})
        self.assertEqual((a["verdict"], a["missing"]), ("partial", ["salary"]))   # notes mention Priya, never her salary
        self.assertEqual(a["not_together"], {"names": ["Priya"], "others": ["salary"]})
        _, a = call("/api/ask", {"question": "What is the wifi password?"})
        self.assertEqual(a["verdict"], "none")
        self.assertFalse(call("/api/ask/ai", {"question": "What is Priya's salary?"})[1]["available"])

    def test_7_switching_off_forgets(self):
        call("/api/allow", {"folder": "photos", "on": False})
        _, s = call("/api/state")
        self.assertEqual(s["notes_in_memory"], 11)
        _, b = call("/api/prep", {"query": "Kofi"})
        self.assertFalse(b["found"])
        self.assertEqual(call("/api/source?id=photos/2026-11-10%20notebook%20page.jpg")[0], 404)
        call("/api/allow", {"folder": "photos", "on": True})

    def test_8_lock_holds(self):
        _, r = call("/api/selftest", {})
        self.assertEqual(len(r["results"]), 6)
        self.assertTrue(all(x["blocked"] and "Blocked by the one rule" in x["note"] for x in r["results"]))

    def test_9_end_leaves_everything_as_it_was(self):
        _, r = call("/api/end", {})
        rec = r["receipt"]
        self.assertEqual((rec["notes_changed"], rec["notes_missing"], rec["sent_off_device"], rec["written_to_disk"]),
                         ([], [], 0, 0))
        self.assertEqual(r["state"]["notes_in_memory"], 0)
        self.assertEqual(tree(ROOT), self.before)           # not one file added, changed or removed


if __name__ == "__main__":
    unittest.main(verbosity=2)
