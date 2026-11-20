"""In-memory search index.

BM25 ranking over small chunks of each note, with light stemming, typo
tolerance and synonym expansion. Rebuilt whenever the approved notes change
and never written to disk.
"""
from __future__ import annotations

import difflib
import math
import re

from . import dates

STOPWORDS = set("""
a about above after again all also am an and any are aren as at be because been before being below
between both but by can cannot could couldn did didn do does doesn doing don down during each few for
from further had hadn has hasn have haven having he her here hers him his how i if in into is isn it
its just ll me more most my no nor not now of off on once only or other our ours out over own re s
same she should shouldn so some such t than that the their theirs them then there these they this
those through to too under until up ve very was wasn we were weren what when where which while who
whom why will with won would wouldn you your yours tell show give find get got know said say says
anything something thing things please re
""".split())

_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")


def stem(w):
    w = w.replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    w = w.replace("'", "")
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("es") and w[-3] in "sxz":
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if len(w) > 6 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 5 and w.endswith("ed"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def words(text):
    return _WORD.findall(text.lower())


def stems(text, keep_stop=False):
    out = []
    for w in words(text):
        if keep_stop or w.replace("’", "'").split("'")[0] not in STOPWORDS:
            out.append(stem(w))
    return out


# Synonym groups used for query expansion (weighted lower than exact terms).
SYNONYMS = [
    "decide decided decision agreed agreement confirmed approved settled",
    "start begin kick-off launch go-live",
    "budget cost spend money price funding",
    "deadline due",
    "lead own owner run",
    "late overdue slipped delayed moved pushed",
    "cancel cancelled dropped scrapped",
    "worry worried concern concerned risk",
]
_SYN = {}
for _group in SYNONYMS:
    _members = {stem(w) for w in words(_group.replace("-", " "))}
    for _m in _members:
        _SYN.setdefault(_m, set()).update(_members - {_m})


class Chunk:
    """A run of consecutive lines from one note, with its date and heading."""
    __slots__ = ("id", "doc", "start", "end", "lines", "date", "heading", "terms", "length")

    def __init__(self, doc, start, lines, date, heading):
        self.doc = doc
        self.start = start                       # 1-based line number
        self.end = start + len(lines) - 1
        self.lines = lines
        self.date = date
        self.heading = heading
        self.id = "%s#%d-%d" % (doc.id, self.start, self.end)
        body = stems(" ".join(lines))
        context = stems(doc.title + " " + (heading or ""))
        self.terms = {}
        for t in body:
            self.terms[t] = self.terms.get(t, 0) + 1.0
        for t in context:                        # title and heading count as context
            self.terms[t] = self.terms.get(t, 0) + 0.6
        self.length = len(body) + 0.6 * len(context)

    @property
    def text(self):
        return "\n".join(self.lines)


def chunk_doc(doc, max_lines=9):
    """Split a note into chunks, tracking dated headings in running notes."""
    chunks = []
    current, start = [], 1
    date, heading = doc.date, None

    def flush():
        nonlocal current, start
        if any(l.strip() for l in current):
            while not current[-1].strip():
                current.pop()
            while not current[0].strip():
                current.pop(0)
                start += 1
            chunks.append(Chunk(doc, start, current, date, heading))
        current = []

    for i, line in enumerate(doc.lines, 1):
        stripped = line.strip()
        is_heading = stripped.startswith("#") or (
            stripped.endswith(":") and len(stripped) < 40 and not stripped.startswith(("-", "*")))
        sec = dates.section_date(line, doc.date)
        if sec or is_heading:
            flush()
            start = i
            if sec:
                date = sec
            heading = stripped.lstrip("# ").rstrip(":")
        elif not stripped:
            if len(current) >= 3:
                flush()
                start = i + 1
                continue
        elif len(current) >= max_lines:
            flush()
            start = i
        if not current:
            start = i
        current.append(line)
    flush()
    return chunks


class Index:
    K1, B = 1.4, 0.6

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.by_id = {c.id: c for c in self.chunks}
        self.df = {}
        for c in self.chunks:
            for t in c.terms:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(self.chunks)
        self.avg_len = (sum(c.length for c in self.chunks) / self.n) if self.n else 1.0
        self._vocab = sorted(self.df)
        self._weak = set()

    def idf(self, term):
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def expand(self, query):
        """Expand a query: ({stem: weight}, {variant: original_stem}).

        Variants are close spellings and synonyms present in the index.
        """
        out, typo, synonym = {}, {}, {}
        self._weak = set()
        for t in dict.fromkeys(stems(query)):
            out[t] = 1.0
            if t not in self.df and len(t) >= 4 and not t.isdigit():
                for near in difflib.get_close_matches(t, self._vocab, n=2, cutoff=0.8):
                    typo[near] = t
            for other in _SYN.get(t, ()):
                if other in self.df:
                    synonym.setdefault(other, t)
        for other, t in synonym.items():
            if other not in out:
                typo.setdefault(other, t)
                self._weak.add(other)
        return out, typo

    def search(self, query, k=10):
        """Return [(chunk, score, matched_query_stems)] best first."""
        q, typo = self.expand(query)
        if not q or not self.n:
            return []
        weak = set(self._weak)                   # synonyms are down-weighted
        results = []
        for c in self.chunks:
            score, matched = 0.0, set()
            for t, w in list(q.items()) + [(near, 0.3 if near in weak else 0.7) for near in typo]:
                tf = c.terms.get(t)
                if not tf:
                    continue
                norm = tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * c.length / self.avg_len))
                score += w * self.idf(t) * norm
                matched.add(typo.get(t, t))
            if score > 0:
                results.append((c, score, matched))
        results.sort(key=lambda r: -r[1])
        return results[:k]

    def coverage(self, query, matched):
        """Fraction of the query's IDF weight matched, and the unmatched stems."""
        q, _ = self.expand(query)
        if not q:
            return 0.0, []
        top = math.log(1 + (self.n + 0.5) / 0.5) if self.n else 1.0
        weight = {t: (self.idf(t) if t in self.df else top) for t in q}
        total = sum(weight.values()) or 1.0
        got = sum(w for t, w in weight.items() if t in matched)
        return got / total, [t for t in q if t not in matched]
