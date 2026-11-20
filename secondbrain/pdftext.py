"""Dependency-free PDF text extraction from in-memory bytes.

Supports Flate, ASCIIHex and ASCII85 streams (with PNG predictors), object
streams, ToUnicode CMaps, WinAnsi encodings with /Differences, and form
XObjects. Text is reconstructed into lines from the text matrix, with
"[page N]" markers between pages.

Encrypted documents raise PDFError("encrypted"). Scanned documents (no text
layer) return their page images for OCR instead.
"""
from __future__ import annotations

import re
import struct
import zlib

MAX_PAGES = 80
MAX_SCAN_IMAGES = 6


class PDFError(Exception):
    pass


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------
class Ref:
    __slots__ = ("num",)

    def __init__(self, num):
        self.num = num


class Name(str):
    pass


class Op(str):
    pass


_WS = b" \t\r\n\f\x00"
_DELIM = b"()<>[]{}/%"
_NUM = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_ESC = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
        ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


class Lexer:
    """Reads PDF values (and, in page content, operators) from bytes."""

    def __init__(self, data, pos=0):
        self.d = data
        self.i = pos
        self.n = len(data)

    def skip(self):
        d, n = self.d, self.n
        while self.i < n:
            c = d[self.i]
            if c in _WS:
                self.i += 1
            elif c == 37:                        # % comment
                while self.i < n and d[self.i] not in b"\r\n":
                    self.i += 1
            else:
                break

    def token(self):
        """Next raw token, or None at the end."""
        self.skip()
        d, i = self.d, self.i
        if i >= self.n:
            return None
        c = d[i]
        if c == 40:                              # (
            return self._literal()
        if c == 60:                              # <
            if d[i + 1:i + 2] == b"<":
                self.i += 2
                return "<<"
            return self._hex()
        if c == 62 and d[i + 1:i + 2] == b">":
            self.i += 2
            return ">>"
        if c in b"[]{}":
            self.i += 1
            return chr(c)
        if c == 47:                              # /Name
            j = i + 1
            while j < self.n and d[j] not in _WS and d[j] not in _DELIM:
                j += 1
            raw = d[i + 1:j]
            self.i = j
            if b"#" in raw:
                raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
            return Name(raw.decode("latin-1"))
        j = i
        while j < self.n and d[j] not in _WS and d[j] not in _DELIM:
            j += 1
        if j == i:                               # stray delimiter
            self.i += 1
            return self.token()
        word = d[i:j]
        self.i = j
        if _NUM.fullmatch(word):
            return float(word) if b"." in word else int(word)
        return Op(word.decode("latin-1"))

    def _literal(self):
        d, n = self.d, self.n
        i = self.i + 1
        depth, out = 1, bytearray()
        while i < n:
            c = d[i]
            if c == 92:                          # backslash
                i += 1
                if i >= n:
                    break
                e = d[i]
                if e in _ESC:
                    out += _ESC[e]
                    i += 1
                elif 48 <= e <= 55:              # octal
                    j = i
                    while j < n and j < i + 3 and 48 <= d[j] <= 55:
                        j += 1
                    out.append(int(d[i:j], 8) & 255)
                    i = j
                elif e in b"\r\n":               # line continuation
                    i += 1
                    if e == 13 and i < n and d[i] == 10:
                        i += 1
                else:
                    out.append(e)
                    i += 1
                continue
            if c == 40:
                depth += 1
            elif c == 41:
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            out.append(c)
            i += 1
        self.i = i
        return bytes(out)

    def _hex(self):
        j = self.d.find(b">", self.i)
        if j < 0:
            j = self.n
        raw = re.sub(rb"[^0-9A-Fa-f]", b"", self.d[self.i + 1:j])
        self.i = j + 1
        if len(raw) % 2:
            raw += b"0"
        return bytes.fromhex(raw.decode("ascii"))

    def value(self, tok=None):
        """A complete value: dict, array, string, name, number or reference."""
        if tok is None:
            tok = self.token()
        if tok == "<<":
            out = {}
            while True:
                k = self.token()
                if k is None or k == ">>":
                    return out
                if isinstance(k, Name):
                    out[str(k)] = self.value()
        if tok == "[":
            out = []
            while True:
                t = self.token()
                if t is None or t == "]":
                    return out
                out.append(self.value(t))
        if isinstance(tok, int):                 # maybe "12 0 R"
            save = self.i
            t2 = self.token()
            if isinstance(t2, int):
                t3 = self.token()
                if t3 == "R":
                    return Ref(tok)
            self.i = save
            return tok
        if isinstance(tok, Op):
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "null":
                return None
        return tok


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------
_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")


class Document:
    def __init__(self, data):
        if not data.lstrip()[:5].startswith(b"%PDF"):
            raise PDFError("not a PDF")
        self.d = data
        self.objs = {}                           # num -> value
        self.streams = {}                        # num -> raw (still encoded) bytes
        self._decoded = {}
        self._scan()
        self._unpack_object_streams()
        if self._encrypted():
            raise PDFError("encrypted")

    def _scan(self):
        d, pos = self.d, 0
        while True:
            m = _OBJ.search(d, pos)
            if not m:
                break
            num = int(m.group(1))
            lex = Lexer(d, m.end())
            try:
                val = lex.value()
            except (IndexError, ValueError):
                pos = m.end()
                continue
            pos = lex.i
            lex.skip()
            if d.startswith(b"stream", lex.i) and isinstance(val, dict):
                start = lex.i + 6
                if d[start:start + 2] == b"\r\n":
                    start += 2
                elif d[start:start + 1] in (b"\n", b"\r"):
                    start += 1
                length = val.get("Length")
                end = -1
                if isinstance(length, int) and 0 <= length <= len(d) - start and \
                        d[start + length:start + length + 30].lstrip()[:9] == b"endstream":
                    end = start + length
                else:
                    end = d.find(b"endstream", start)
                    if end < 0:
                        end = len(d)
                    while end > start and d[end - 1] in b"\r\n":
                        end -= 1
                self.streams[num] = d[start:end]
                pos = end
            self.objs[num] = val

    def _unpack_object_streams(self):
        for num, val in list(self.objs.items()):
            if not (isinstance(val, dict) and val.get("Type") == "ObjStm"):
                continue
            try:
                data = self.stream(num)
            except PDFError:
                continue
            n, first = self.get(val.get("N"), 0), self.get(val.get("First"), 0)
            head = Lexer(data, 0)
            pairs = []
            for _ in range(int(n)):
                a, b = head.token(), head.token()
                if not isinstance(a, int) or not isinstance(b, int):
                    break
                pairs.append((a, b))
            for objnum, off in pairs:
                if objnum in self.objs:
                    continue                     # direct definitions take precedence
                try:
                    self.objs[objnum] = Lexer(data, int(first) + off).value()
                except (IndexError, ValueError):
                    pass

    def _encrypted(self):
        for m in re.finditer(rb"trailer", self.d):
            try:
                t = Lexer(self.d, m.end()).value()
            except (IndexError, ValueError):
                continue
            if isinstance(t, dict) and "Encrypt" in t:
                return True
        return any(isinstance(v, dict) and v.get("Type") == "XRef" and "Encrypt" in v for v in self.objs.values())

    def get(self, v, default=None, depth=0):
        while isinstance(v, Ref) and depth < 20:
            v = self.objs.get(v.num)
            depth += 1
        return default if v is None else v

    def stream(self, num):
        """Decoded bytes of stream object `num`."""
        if num in self._decoded:
            return self._decoded[num]
        raw = self.streams.get(num)
        if raw is None:
            raise PDFError("missing stream")
        info = self.objs.get(num) or {}
        filters = self.get(info.get("Filter"), [])
        if not isinstance(filters, list):
            filters = [filters]
        params = self.get(info.get("DecodeParms"), [])
        if not isinstance(params, list):
            params = [params]
        data = raw
        for k, f in enumerate(self.get(x) for x in filters):
            p = self.get(params[k]) if k < len(params) else None
            if f in ("FlateDecode", "Fl"):
                data = _inflate(data)
                if isinstance(p, dict) and self.get(p.get("Predictor"), 1) >= 10:
                    data = _png_unpredict(data, self.get(p.get("Columns"), 1),
                                          self.get(p.get("Colors"), 1), self.get(p.get("BitsPerComponent"), 8))
            elif f in ("ASCIIHexDecode", "AHx"):
                data = bytes.fromhex(re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0]).decode() or "")
            elif f in ("ASCII85Decode", "A85"):
                data = _a85(data)
            elif f in ("DCTDecode", "DCT", "JPXDecode"):
                break                            # image data: leave encoded
            else:
                raise PDFError("unsupported compression %s" % f)
        self._decoded[num] = data
        return data

    # ---- pages ----------------------------------------------------------
    def pages(self):
        """[(page dict, resources)] in reading order."""
        root = None
        for m in re.finditer(rb"trailer", self.d):
            try:
                t = Lexer(self.d, m.end()).value()
                if isinstance(t, dict) and "Root" in t:
                    root = self.get(t["Root"])
            except (IndexError, ValueError):
                pass
        if root is None:
            for v in self.objs.values():
                if isinstance(v, dict) and v.get("Type") == "XRef" and "Root" in v:
                    root = self.get(v["Root"])
        if root is None:
            root = next((v for v in self.objs.values() if isinstance(v, dict) and v.get("Type") == "Catalog"), None)
        out, seen = [], set()

        def walk(node, inherited, depth):
            node_d = self.get(node)
            if not isinstance(node_d, dict) or depth > 30 or len(out) >= MAX_PAGES:
                return
            key = node.num if isinstance(node, Ref) else id(node_d)
            if key in seen:
                return
            seen.add(key)
            res = node_d.get("Resources", inherited)
            kids = self.get(node_d.get("Kids"))
            if isinstance(kids, list):
                for k in kids:
                    walk(k, res, depth + 1)
            elif node_d.get("Type") in ("Page", None) or "Contents" in node_d:
                out.append((node_d, self.get(res, {})))

        if isinstance(root, dict) and "Pages" in root:
            walk(root["Pages"], {}, 0)
        if not out:                              # damaged page tree: collect page objects directly
            for num in sorted(self.objs):
                v = self.objs[num]
                if isinstance(v, dict) and v.get("Type") == "Page":
                    out.append((v, self.get(v.get("Resources"), {})))
                    if len(out) >= MAX_PAGES:
                        break
        return out

    def contents(self, page):
        c = page.get("Contents")
        items = self.get(c)
        refs = items if isinstance(items, list) else [c]
        parts = []
        for r in refs:
            if isinstance(r, Ref) and r.num in self.streams:
                try:
                    parts.append(self.stream(r.num))
                except PDFError:
                    pass
        return b"\n".join(parts)


def _inflate(data):
    d = zlib.decompressobj()
    try:
        return d.decompress(data) + d.flush()
    except zlib.error:
        # Some writers produce slightly damaged streams: keep what decodes.
        d, out = zlib.decompressobj(), bytearray()
        for i in range(0, len(data), 256):
            try:
                out += d.decompress(data[i:i + 256])
            except zlib.error:
                break
        if not out:
            try:
                return zlib.decompress(data, -15)
            except zlib.error:
                raise PDFError("damaged stream")
        return bytes(out)


def _png_unpredict(data, columns, colors, bpc):
    bpp = max(1, colors * bpc // 8)
    row = (columns * colors * bpc + 7) // 8
    out, prev = bytearray(), bytearray(row)
    for i in range(0, len(data), row + 1):
        ft, cur = data[i], bytearray(data[i + 1:i + 1 + row])
        for x in range(len(cur)):
            a = cur[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            if ft == 1:
                cur[x] = (cur[x] + a) & 255
            elif ft == 2:
                cur[x] = (cur[x] + b) & 255
            elif ft == 3:
                cur[x] = (cur[x] + (a + b) // 2) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                cur[x] = (cur[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += cur
        prev = cur
    return bytes(out)


def _a85(data):
    data = re.sub(rb"\s", b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    data = data.split(b"~>")[0]
    out, group = bytearray(), []
    for c in data:
        if c == ord("z") and not group:
            out += b"\0\0\0\0"
            continue
        group.append(c - 33)
        if len(group) == 5:
            n = 0
            for g in group:
                n = n * 85 + g
            out += struct.pack(">I", n & 0xFFFFFFFF)
            group = []
    if group:
        k = len(group)
        group += [84] * (5 - k)
        n = 0
        for g in group:
            n = n * 85 + g
        out += struct.pack(">I", n & 0xFFFFFFFF)[:k - 1]
    return bytes(out)


# ---------------------------------------------------------------------------
# Fonts and encodings
# ---------------------------------------------------------------------------
_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "'", "quoteright": "’", "quoteleft": "‘", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "minus": "-", "period": ".",
    "slash": "/", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\", "bracketright": "]",
    "underscore": "_", "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~", "bullet": "•",
    "endash": "–", "emdash": "—", "quotedblleft": "“", "quotedblright": "”",
    "sterling": "£", "Euro": "€", "fi": "fi", "fl": "fl", "ellipsis": "…", "degree": "°",
    "checkmark": "✓", "copyright": "©", "registered": "®", "trademark": "™",
}
# Windows-1252 differs from Latin-1 only in 0x80-0x9F.
_CP1252 = {0x80: "€", 0x82: "‚", 0x84: "„", 0x85: "…", 0x86: "†", 0x87: "‡",
           0x89: "‰", 0x8A: "Š", 0x8B: "‹", 0x8C: "Œ", 0x91: "‘", 0x92: "’",
           0x93: "“", 0x94: "”", 0x95: "•", 0x96: "–", 0x97: "—", 0x99: "™",
           0x9A: "š", 0x9B: "›", 0x9C: "œ"}


def _glyph(name):
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4,6})", name) or re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return ""
    base = name.split(".")[0].split("_")[0]
    return _GLYPHS.get(base, base if len(base) == 1 else "")


def _utf16(b):
    try:
        return b.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _parse_cmap(data):
    """ToUnicode map: {code bytes: text}, plus how many bytes a code takes."""
    out, widths = {}, set()
    lex = Lexer(data)
    toks = []
    while True:
        t = lex.token()
        if t is None:
            break
        if t == "[":
            arr = []
            while True:
                t2 = lex.token()
                if t2 is None or t2 == "]":
                    break
                arr.append(t2)
            toks.append(arr)
        else:
            toks.append(t)
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "begincodespacerange":
            i += 1
            while i + 1 < len(toks) and toks[i] != "endcodespacerange":
                if isinstance(toks[i], bytes):
                    widths.add(len(toks[i]))
                i += 2
        elif t == "beginbfchar":
            i += 1
            while i + 1 < len(toks) and toks[i] != "endbfchar":
                src, dst = toks[i], toks[i + 1]
                if isinstance(src, bytes) and isinstance(dst, bytes):
                    out[src] = _utf16(dst)
                    widths.add(len(src))
                i += 2
        elif t == "beginbfrange":
            i += 1
            while i + 2 < len(toks) and toks[i] != "endbfrange":
                lo, hi, dst = toks[i], toks[i + 1], toks[i + 2]
                if isinstance(lo, bytes) and isinstance(hi, bytes):
                    n = len(lo)
                    a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
                    widths.add(n)
                    if b - a <= 65536:
                        if isinstance(dst, list):
                            for k, item in enumerate(dst[: b - a + 1]):
                                if isinstance(item, bytes):
                                    out[(a + k).to_bytes(n, "big")] = _utf16(item)
                        elif isinstance(dst, bytes) and dst:
                            base = int.from_bytes(dst, "big")
                            for k in range(b - a + 1):
                                v = (base + k).to_bytes(len(dst), "big")
                                out[(a + k).to_bytes(n, "big")] = _utf16(v)
                i += 3
        else:
            i += 1
    return out, (min(widths) if widths else 1)


class Font:
    def __init__(self, doc, fd):
        self.cmap, self.code_len = {}, 1
        self.base = {}
        self.widths, self.default_width = {}, 500.0
        fd = doc.get(fd, {})
        if not isinstance(fd, dict):
            fd = {}
        sub = fd.get("Subtype")
        self.cid = sub == "Type0"
        tu = fd.get("ToUnicode")
        if isinstance(tu, Ref) and tu.num in doc.streams:
            try:
                self.cmap, self.code_len = _parse_cmap(doc.stream(tu.num))
            except PDFError:
                pass
        if self.cid:
            self.code_len = 2 if not self.cmap else self.code_len
            desc = doc.get(fd.get("DescendantFonts"), [])
            desc = doc.get(desc[0], {}) if isinstance(desc, list) and desc else {}
            self.default_width = float(doc.get(desc.get("DW"), 1000))
            w = doc.get(desc.get("W"), [])
            if isinstance(w, list):
                i = 0
                while i < len(w):
                    first = doc.get(w[i])
                    nxt = doc.get(w[i + 1]) if i + 1 < len(w) else None
                    if isinstance(nxt, list):
                        for k, val in enumerate(nxt):
                            self.widths[int(first) + k] = float(doc.get(val, 0))
                        i += 2
                    elif i + 2 < len(w):
                        for c in range(int(first), int(nxt) + 1):
                            self.widths[c] = float(doc.get(w[i + 2], 0))
                        i += 3
                    else:
                        break
        else:
            first = int(doc.get(fd.get("FirstChar"), 0))
            ws = doc.get(fd.get("Widths"), [])
            if isinstance(ws, list):
                for k, val in enumerate(ws):
                    self.widths[first + k] = float(doc.get(val, 0))
            desc = doc.get(fd.get("FontDescriptor"), {})
            if isinstance(desc, dict) and "MissingWidth" in desc:
                self.default_width = float(doc.get(desc["MissingWidth"], 500))
            enc = doc.get(fd.get("Encoding"))
            if isinstance(enc, dict):
                diffs = doc.get(enc.get("Differences"), [])
                code = 0
                for item in diffs if isinstance(diffs, list) else []:
                    item = doc.get(item)
                    if isinstance(item, int):
                        code = item
                    elif isinstance(item, Name):
                        self.base[code] = _glyph(str(item))
                        code += 1

    def decode(self, raw):
        """[(text, code)] for the bytes of one shown string."""
        out, n, i = [], self.code_len, 0
        while i < len(raw):
            if self.cmap:
                for k in (n, 1, 2, 3, 4):
                    chunk = raw[i:i + k]
                    if len(chunk) == k and chunk in self.cmap:
                        out.append((self.cmap[chunk], int.from_bytes(chunk, "big")))
                        i += k
                        break
                else:
                    chunk = raw[i:i + n]
                    code = int.from_bytes(chunk, "big")
                    out.append((self.base.get(code, "" if self.cid else _latin(code)), code))
                    i += n
            elif self.cid:
                code = int.from_bytes(raw[i:i + 2], "big")
                out.append(("", code))           # no ToUnicode map: unknown glyph
                i += 2
            else:
                code = raw[i]
                out.append((self.base.get(code) or _latin(code), code))
                i += 1
        return out

    def width(self, code):
        return self.widths.get(code, self.default_width) / 1000.0


def _latin(code):
    if code in _CP1252:
        return _CP1252[code]
    if code < 32 or 0x7F <= code < 0xA0:
        return ""
    return chr(code)


# ---------------------------------------------------------------------------
# Content streams
# ---------------------------------------------------------------------------
class _Text:
    def __init__(self):
        self.lines, self.cur = [], []
        self.last_y = None
        self.end_x = None                        # x position after the last text run
        self.seen_text = False

    def newline(self):
        line = "".join(self.cur).rstrip()
        if line or (self.lines and self.lines[-1]):
            self.lines.append(line)
        self.cur = []
        self.end_x = None

    def put(self, s, x0, y, size):
        if not s:
            return
        if self.last_y is not None and abs(y - self.last_y) > max(1.0, 0.45 * size):
            self.newline()
            if abs(y - self.last_y) > 1.9 * size and self.lines and self.lines[-1]:
                self.lines.append("")            # paragraph gap
        elif self.end_x is not None and self.cur:
            gap = x0 - self.end_x
            if gap > 0.18 * size and not self.cur[-1].endswith(" ") and not s.startswith(" "):
                self.cur.append(" ")
            elif gap < -3 * size:                # large leftward jump: new column
                self.newline()
        self.cur.append(s)
        self.last_y = y
        self.seen_text = True


def _mul(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def _run(doc, data, resources, out, images, depth=0):
    res = doc.get(resources, {})
    if not isinstance(res, dict):
        res = {}
    fonts_d = doc.get(res.get("Font"), {})
    xobjs = doc.get(res.get("XObject"), {})
    cache = {}

    def font(name):
        if name not in cache:
            cache[name] = Font(doc, fonts_d.get(name) if isinstance(fonts_d, dict) else None)
        return cache[name]

    lex = Lexer(data)
    stack = []
    ctm = (1, 0, 0, 1, 0, 0)
    gstack = []
    tm = lm = (1, 0, 0, 1, 0, 0)
    f, size, tc, tw, th, tl = None, 12.0, 0.0, 0.0, 1.0, 0.0

    def show(raw):
        nonlocal tm
        if f is None:
            return
        trm = _mul(tm, ctm)
        scale = (abs(trm[3]) or abs(trm[1]) or 1.0)
        x0, y = trm[4], trm[5]
        text, adv = [], 0.0
        for ch, code in f.decode(raw):
            text.append(ch)
            w = f.width(code) * size + tc + (tw if (code == 32 and f.code_len == 1) else 0)
            adv += w * th
        tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + adv * tm[0], tm[5] + adv * tm[1])
        s = "".join(text)
        out.put(s, x0, y, size * scale)
        out.end_x = _mul(tm, ctm)[4]

    while True:
        t = lex.token()
        if t is None:
            break
        if not isinstance(t, Op):
            if t == "[":
                arr = []
                while True:
                    x = lex.token()
                    if x is None or x == "]":
                        break
                    arr.append(x)
                stack.append(arr)
            elif t == "<<":
                lex.i -= 2
                stack.append(lex.value())
            else:
                stack.append(t)
            continue
        op = str(t)
        try:
            if op == "BT":
                tm = lm = (1, 0, 0, 1, 0, 0)
            elif op == "Tf" and len(stack) >= 2:
                f, size = font(str(stack[-2])), float(stack[-1])
            elif op == "Tc" and stack:
                tc = float(stack[-1])
            elif op == "Tw" and stack:
                tw = float(stack[-1])
            elif op == "Tz" and stack:
                th = float(stack[-1]) / 100.0
            elif op == "TL" and stack:
                tl = float(stack[-1])
            elif op in ("Td", "TD") and len(stack) >= 2:
                tx, ty = float(stack[-2]), float(stack[-1])
                if op == "TD":
                    tl = -ty
                lm = _mul((1, 0, 0, 1, tx, ty), lm)
                tm = lm
            elif op == "Tm" and len(stack) >= 6:
                lm = tm = tuple(float(v) for v in stack[-6:])
            elif op == "T*":
                lm = _mul((1, 0, 0, 1, 0, -tl), lm)
                tm = lm
            elif op == "Tj" and stack and isinstance(stack[-1], bytes):
                show(stack[-1])
            elif op in ("'", '"') and stack and isinstance(stack[-1], bytes):
                if op == '"' and len(stack) >= 3:
                    tw, tc = float(stack[-3]), float(stack[-2])
                lm = _mul((1, 0, 0, 1, 0, -tl), lm)
                tm = lm
                show(stack[-1])
            elif op == "TJ" and stack and isinstance(stack[-1], list):
                for item in stack[-1]:
                    if isinstance(item, bytes):
                        show(item)
                    elif isinstance(item, (int, float)):
                        shift = -float(item) / 1000.0 * size * th
                        tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + shift * tm[0], tm[5] + shift * tm[1])
                        if -float(item) > 180 and out.cur and not out.cur[-1].endswith(" "):
                            out.cur.append(" ")
            elif op == "cm" and len(stack) >= 6:
                ctm = _mul(tuple(float(v) for v in stack[-6:]), ctm)
            elif op == "q":
                gstack.append(ctm)
            elif op == "Q" and gstack:
                ctm = gstack.pop()
            elif op == "Do" and stack and isinstance(xobjs, dict):
                ref = xobjs.get(str(stack[-1]))
                xo = doc.get(ref, {})
                if isinstance(xo, dict) and isinstance(ref, Ref):
                    if xo.get("Subtype") == "Form" and depth < 6:
                        try:
                            inner = doc.stream(ref.num)
                        except PDFError:
                            inner = b""
                        m = xo.get("Matrix")
                        saved = ctm
                        if isinstance(m, list) and len(m) == 6:
                            ctm = _mul(tuple(float(doc.get(v, 0)) for v in m), ctm)
                        _run(doc, inner, xo.get("Resources", res), out, images, depth + 1)
                        ctm = saved
                    elif xo.get("Subtype") == "Image" and images is not None:
                        images.append(ref.num)
            elif op == "BI":                     # skip inline image data
                j = data.find(b"ID", lex.i)
                k = data.find(b"EI", j + 2) if j >= 0 else -1
                while k > 0 and not (data[k - 1:k] in (b" ", b"\n", b"\r") and
                                     (k + 2 >= len(data) or data[k + 2] in _WS)):
                    k = data.find(b"EI", k + 2)
                lex.i = len(data) if k < 0 else k + 2
        except (TypeError, ValueError, IndexError):
            pass
        stack = []


# ---------------------------------------------------------------------------
# Scanned documents
# ---------------------------------------------------------------------------
def _png(width, height, colors, pixels):
    def chunk(kind, body):
        c = struct.pack(">I", len(body)) + kind + body
        return c + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    row = width * colors
    raw = b"".join(b"\0" + pixels[y * row:(y + 1) * row] for y in range(height))
    ctype = {1: 0, 3: 2}[colors]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _image_bytes(doc, num):
    info = doc.objs.get(num) or {}
    w, h = int(doc.get(info.get("Width"), 0)), int(doc.get(info.get("Height"), 0))
    if w < 300 or h < 300:
        return None                              # skip small images (logos, icons)
    filters = doc.get(info.get("Filter"), [])
    filters = filters if isinstance(filters, list) else [filters]
    filters = [str(doc.get(x)) for x in filters]
    if filters and filters[-1] in ("DCTDecode", "DCT"):
        try:
            data = doc.stream(num)
        except PDFError:
            return None
        return data if data[:2] == b"\xff\xd8" else None
    if filters and filters[-1] not in ("FlateDecode", "Fl"):
        return None
    cs = doc.get(info.get("ColorSpace"))
    colors = {"DeviceRGB": 3, "DeviceGray": 1, "CalRGB": 3, "CalGray": 1}.get(str(cs))
    if colors is None and isinstance(cs, list) and cs and str(doc.get(cs[0])) == "ICCBased":
        icc = doc.get(cs[1], {}) if len(cs) > 1 else {}
        colors = int(doc.get(icc.get("N"), 3)) if isinstance(icc, dict) else 3
        colors = colors if colors in (1, 3) else None
    if colors is None or int(doc.get(info.get("BitsPerComponent"), 8)) != 8:
        return None
    try:
        pixels = doc.stream(num)
    except PDFError:
        return None
    if len(pixels) < w * h * colors:
        return None
    return _png(w, h, colors, pixels)


# ---------------------------------------------------------------------------
def extract(data):
    """Return (text, page_images).

    `page_images` (JPEG or PNG bytes, at most MAX_SCAN_IMAGES) is only filled for
    scanned documents with little or no text layer.
    """
    doc = Document(data)
    pages = doc.pages()
    if not pages:
        raise PDFError("no pages found")
    all_lines, image_refs = [], []
    for n, (page, res) in enumerate(pages, 1):
        out = _Text()
        _run(doc, doc.contents(page), res, out, image_refs)
        out.newline()
        lines = [l.replace("\t", " ") for l in out.lines]
        while lines and not lines[-1]:
            lines.pop()
        if len(pages) > 1:
            if all_lines:
                all_lines.append("")
            all_lines.append("[page %d]" % n)
        all_lines.extend(lines)
    text = "\n".join(all_lines)
    letters = len(re.sub(r"\s|\[page \d+\]", "", text))
    images = []
    if letters < 20 * len(pages):               # little or no text layer: likely a scan
        seen = set()
        for num in image_refs:
            if num in seen:
                continue
            seen.add(num)
            img = _image_bytes(doc, num)
            if img:
                images.append(img)
            if len(images) >= MAX_SCAN_IMAGES:
                break
        if images and letters < 5 * len(pages):
            text = ""
    return text, images
