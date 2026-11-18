"""The two things the assistant does: prepare you for a meeting, and find an answer.

Each has two layers:

  1. An instant layer built from pattern rules and search. Every item is a
     real line from a real note, with its file, line number and date.
  2. A written summary from the local model, which is only allowed to use the
     same numbered excerpts, and whose every point is checked back against
     them before you see it (`_ground`). Points it cannot back up are dropped.

Nothing is cached between requests: the server recomputes from memory each time.
"""
from __future__ import annotations

import re

from . import dates, extract, llm
from .index import stems, STOPWORDS
from .session import LOOSE, _unit

STALE_DAYS = 21
OLD_NOTE_DAYS = 30


# ---- retrieval -------------------------------------------------------------
def _query_vector(session, query):
    em = session.model.status.get("embed")
    if not em or not session.vectors:
        return None
    try:
        return _unit(session.model.embed(em, [query], purpose="meaning search (your question)")[0])
    except llm.ModelError:
        return None


def _retrieve(session, query, qv, k=12):
    """Keyword ranking, blended with meaning-based ranking when available."""
    idx = session.index
    lexical = idx.search(query, k=40)
    hits = {}
    for rank, (c, score, matched) in enumerate(lexical):
        cov, _ = idx.coverage(query, matched)
        hits[c.id] = {"chunk": c, "matched": matched, "coverage": cov, "sim": None, "fused": 1.0 / (60 + rank),
                      "strong": rank < 4 and cov >= 0.75}
    if qv is not None:
        sims = sorted(((sum(a * b for a, b in zip(qv, v)), cid) for cid, v in session.vectors.items()
                       if cid in idx.by_id), reverse=True)[:k]
        for rank, (sim, cid) in enumerate(sims):
            if cid in hits:
                hits[cid]["sim"] = sim
                hits[cid]["fused"] += 1.0 / (60 + rank)
            elif sim >= 0.5:
                hits[cid] = {"chunk": idx.by_id[cid], "matched": set(), "coverage": 0.0, "sim": sim,
                             "fused": 1.0 / (60 + rank), "strong": False}
    # Notes that contain the words you actually used always come first;
    # meaning-based matches fill in behind them.
    return sorted(hits.values(), key=lambda h: (not h["strong"], -h["fused"]))


class _Evidence:
    """Numbers the excerpts an answer relies on, so everything can cite [n]."""

    def __init__(self, today):
        self.today, self.items, self._n = today, [], {}

    def cite(self, chunk, why=None):
        if chunk.id not in self._n:
            d = chunk.doc
            self._n[chunk.id] = len(self.items) + 1
            self.items.append({
                "n": len(self.items) + 1, "doc": d.id, "title": d.title, "kind": d.kind,
                "image_kind": d.image_kind, "uncertain": d.uncertain,
                "date": chunk.date.isoformat(), "date_label": dates.human(chunk.date, self.today),
                "ago": dates.ago(chunk.date, self.today), "date_source": d.date_source,
                "start": chunk.start, "end": chunk.end, "heading": chunk.heading, "text": chunk.text,
                "why": why or "",
            })
        return self._n[chunk.id]


def _honesty(session, extra=()):
    """Plain statements about what the assistant could not see or is unsure of."""
    notes = list(extra)
    off = [u for u in session.units() if u not in session.allowed]
    if off:
        notes.append("I have not looked in %s you switched off: %s. The answer could be different with %s on."
                     % ("the folder" if len(off) == 1 else "%d folders" % len(off), ", ".join(off),
                        "it" if len(off) == 1 else "them"))
    if session.hidden:
        notes.append("%d individual note%s hidden from me." % (len(session.hidden), " is" if len(session.hidden) == 1 else "s are"))
    unread = [d for d in session.docs.values() if d.kind == "image" and d.status in ("no_model", "failed")]
    if unread:
        notes.append("I could not read %d image%s (%s), so anything written only there is missing."
                     % (len(unread), "" if len(unread) == 1 else "s", unread[0].note or "no local vision model"))
    pending = session.busy()
    if pending:
        notes.append("I am still reading %d image%s. Ask again in a moment for a fuller answer."
                     % (pending, "" if pending == 1 else "s"))
    return notes


def _item(ev, chunk, offset, text, why=None, **more):
    out = {"text": text, "n": ev.cite(chunk, why), "doc": chunk.doc.id, "line": chunk.start + offset,
           "date": chunk.date.isoformat(), "date_label": dates.human(chunk.date, ev.today),
           "ago": dates.ago(chunk.date, ev.today), "handwriting": chunk.doc.kind == "image",
           "uncertain": chunk.doc.kind == "image" and ("[?]" in text or "[illegible]" in text)}
    out.update(more)
    return out


# ---- meeting prep ----------------------------------------------------------
def prep(session, query):
    query = (query or "").strip()
    session.sync(force=True)
    qv = _query_vector(session, query)
    with session.lock:
        idx, today = session.index, session.today
        ev = _Evidence(today)
        q, typo = idx.expand(query)
        terms = set(q) | set(typo)
        result = {"query": query, "open_actions": [], "probably_done": [], "done": [], "decisions": [],
                  "changes": [], "questions": [], "recent": [], "evidence": ev.items, "honesty": [], "terms": sorted(terms),
                  "notes_used": 0, "found": False}
        if not terms:
            result["honesty"] = ["Tell me who or what the meeting is about."]
            return result

        hits = _retrieve(session, query, qv)
        # For a short subject like a name, "similar in meaning" is not evidence: "Kofi" must
        # actually appear. Meaning-only matches count only for longer, descriptive requests.
        relevant = [h for h in hits if h["coverage"] >= 0.5 or ((h["sim"] or 0) >= 0.6 and len(q) >= 3)]
        if not relevant:
            missing = ", ".join(_as_typed(query, sorted(q)))
            result["honesty"] = _honesty(session, ["Nothing I am allowed to see mentions “%s”." % missing])
            return result
        result["found"] = True

        # Lines worth showing: in a note that is about the subject, or naming it.
        lines = []                                   # (chunk, offset, text, classified)
        for h in relevant:
            c = h["chunk"]
            whole = bool(terms & set(stems(c.doc.title + " " + (c.heading or "")))) or not h["matched"]
            for off, line in enumerate(c.lines):
                if whole or terms & set(stems(line)):
                    lines.append((c, off, line, extract.classify_line(line, c.date)))

        # What changed between older and newer notes.
        facts = [((c, off, line), line, c.date) for c, off, line, _ in lines]
        superseded, moved = {}, set()
        names = set(stems(" ".join(extract.find_people(session.docs.values()))))
        for old, new, kind, old_v, new_v in extract.find_changes(facts, ignore=terms | names):
            o = _item(ev, old[0], old[1], extract._clean(old[2]))
            n = _item(ev, new[0], new[1], extract._clean(new[2]))
            result["changes"].append({"kind": kind, "old_value": old_v, "new_value": new_v, "old": o, "new": n})
            superseded[(old[0].id, old[1])] = n
            if kind == "date":
                moved.add((old[0].id, old[1]))

        # Every dated line anywhere that could report an action as done.
        reports = [(c, off, line) for c in idx.chunks for off, line in enumerate(c.lines)
                   if extract._DONE_ANY.search(line)]

        for c, off, line, info in sorted(lines, key=lambda x: (x[0].date, x[0].doc.id, x[0].start + x[1]), reverse=True):
            if not info:
                continue
            if "action" in info["kinds"]:
                due = info.get("due")
                it = _item(ev, c, off, info["text"], owner=info.get("owner"),
                           due=due.isoformat() if due else None,
                           due_label=dates.human(due, today) if due else None)
                if info["done"]:
                    result["done"].append(it)
                    continue
                later = None
                for rc, roff, rline in reports:
                    ticked = "✓" in rline or "✔" in rline or "[x]" in rline.lower()
                    # a later note, or a tick: the same day's "get it signed" is a plan, not a report
                    if (rc.id, roff) != (c.id, off) and (rc.date > c.date or (ticked and rc.date == c.date)) and extract.resolves(
                            info["text"], rline + " " + rc.doc.title + " " + (rc.heading or ""), info.get("owner")):
                        if later is None or rc.date > later[0].date:
                            later = (rc, roff, rline)
                if not later and (c.id, off) in moved:
                    it["resolved_by"] = superseded[(c.id, off)]      # a newer note overtakes this one
                    result["probably_done"].append(it)
                    continue
                if later:
                    it["resolved_by"] = _item(ev, later[0], later[1], extract._clean(later[2]))
                    result["probably_done"].append(it)
                    continue
                age = (today - c.date).days
                it["overdue"] = bool(due and due < today)
                it["stale"] = age > STALE_DAYS
                result["open_actions"].append(it)
            if "decision" in info["kinds"]:
                it = _item(ev, c, off, info["text"])
                if (c.id, off) in superseded:
                    it["superseded_by"] = superseded[(c.id, off)]
                result["decisions"].append(it)
            if "question" in info["kinds"]:
                result["questions"].append(_item(ev, c, off, info["text"]))

        result["changes"] = _unique_changes(result["changes"])
        result["open_actions"].sort(key=lambda a: not a["overdue"])
        for key, cap in (("open_actions", 10), ("probably_done", 5), ("done", 5), ("decisions", 8), ("questions", 6)):
            result[key] = result[key][:cap]

        newest = sorted(relevant, key=lambda h: h["chunk"].date, reverse=True)
        for h in newest[:3]:
            c = h["chunk"]
            result["recent"].append(_item(ev, c, 0, _snippet(c, terms), why=_why(h)))
        for h in relevant[:10]:                      # make sure the best matches are citable
            ev.cite(h["chunk"], _why(h))

        docs = {e["doc"] for e in ev.items}
        result["notes_used"] = len(docs)
        latest = newest[0]["chunk"].date
        extra = []
        if (today - latest).days > OLD_NOTE_DAYS:
            extra.append("The newest note I found on this is from %s (%s). It may be out of date."
                         % (dates.human(latest, today), dates.ago(latest, today)))
        if any(e["kind"] == "image" for e in ev.items):
            extra.append("Some of this comes from handwriting or a screenshot I read myself. I can misread words, "
                         "so open the original before relying on it.")
        if not (result["open_actions"] or result["decisions"] or result["questions"] or result["changes"]):
            extra.append("I found notes that mention this, but no clear actions, decisions or open questions in them.")
        result["honesty"] = _honesty(session, extra)
        return result


def _as_typed(query, stem_list):
    """Show the user's own words back to them, not our internal word stems."""
    from .index import words, stem, STOPWORDS
    back = {}
    for w in words(query):
        if w not in STOPWORDS:
            back.setdefault(stem(w), w)
    return [back.get(s, s) for s in stem_list]


def _why(h):
    if h["matched"]:
        return "mentions " + ", ".join(sorted(h["matched"]))   # word stems, e.g. "decid" for decided/decision
    return "similar in meaning to what you asked (no matching words)"


def _snippet(chunk, terms, width=220):
    best = max(chunk.lines, key=lambda l: (len(terms & set(stems(l))), len(l.strip()) > 20))
    text = extract._clean(best) or extract._clean(chunk.text)
    return text[:width] + ("…" if len(text) > width else "")


# ---- find an answer --------------------------------------------------------
def ask(session, question):
    question = (question or "").strip()
    session.sync(force=True)
    qv = _query_vector(session, question)
    with session.lock:
        idx, today = session.index, session.today
        ev = _Evidence(today)
        result = {"question": question, "verdict": "none", "snippets": [], "changes": [], "missing": [],
                  "evidence": ev.items, "honesty": [], "terms": []}
        q, typo = idx.expand(question)
        terms = set(q) | set(typo)
        result["terms"] = sorted(terms)
        if not terms:
            result["honesty"] = ["Ask me something specific, like “what did we decide about the pilot date?”"]
            return result
        if not idx.n:
            result["honesty"] = _honesty(session, ["I cannot see any notes yet. Switch on a folder first."])
            return result

        hits = _retrieve(session, question, qv)[:8]
        union = set()
        for h in hits[:3]:
            union |= h["matched"]
        cov, missing = idx.coverage(question, union)
        # for a short question, "similar in meaning" alone is not evidence (as in meeting prep)
        top_sim = max([h["sim"] or 0 for h in hits] or [0]) if len(q) >= 3 else 0
        best_single = max([h["coverage"] for h in hits] or [0])
        if cov >= 0.75 and best_single >= 0.5:
            verdict = "found"
        elif cov >= 0.45 or top_sim >= 0.62:
            verdict = "partial"
        else:
            verdict = "none"
        result["verdict"] = verdict
        missing = _as_typed(question, missing)
        result["missing"] = missing

        keep = [h for h in hits if h["coverage"] >= 0.34 or ((h["sim"] or 0) >= 0.5 and len(q) >= 3)][:6]
        asked = set(q)

        def said(line):                              # which of the asked-about words a line covers
            got = set(stems(line))
            return {t for t in asked if t in got} | {typo[t] for t in typo if t in got}

        need = max(1, (len(asked) + 1) // 2)
        facts = []
        # A question that names something ("the Corvid pilot") is only answered by lines about
        # that thing. Corvid in one note and "pilot start" in another is not an answer.
        names = {t: w for t, w in _names_in(session, question, asked).items() if t in idx.df}
        # "decide", "say", "plan"… are how you ask, not what you ask about: an answer need not repeat them
        others = asked - set(names) - _ASKING_WORDS
        if names:
            keep = []
            # Words the notes use as names (Helix, Corvid, Priya…). A line naming a different one
            # is about that, not about what you asked.
            others_named = _name_words(session) - set(names)
            weak = getattr(idx, "_weak", set())

            ranked = asked - set(names)               # for ranking, every word counts

            def weigh(got):
                score, direct = 0.0, ranked & got
                score += len(direct) - 0.5 * len(direct & _ASKING_WORDS)
                for near, orig in typo.items():
                    if near in got and orig in ranked and orig not in direct:
                        score += 0.4 if near in weak else 0.8
                        direct = direct | {orig}
                return score

            name_lines = []
            in_doc = {}
            for c in idx.chunks:
                if c.doc.id not in in_doc:            # does this note mention it anywhere?
                    in_doc[c.doc.id] = set(stems(c.doc.title + "\n" + "\n".join(c.doc.lines)))
                if not all(n in in_doc[c.doc.id] for n in names):
                    continue
                around = set(stems(c.doc.title + " " + (c.heading or "")))
                whole_note = all(n in around for n in names) and not (around & others_named)
                for off, line in enumerate(c.lines):
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    got = set(stems(line)) | said(line)
                    full = others <= said(line)            # every other thing you asked about is in this line
                    if all(n in got for n in names):
                        bonus = 0.3                        # names it outright
                    elif got & others_named:
                        continue                           # "Helix pilot start…" is not about Corvid
                    elif whole_note or full:
                        bonus = 0.0                        # in a note about it
                    else:
                        continue
                    name_lines.append((weigh(got), bonus, full, c.date, c, off, line))
            name_lines.sort(key=lambda x: (x[0] >= 0.8, x[0] + x[1], x[3]), reverse=True)
            together = [x for x in name_lines if (x[2] if others else True) or x[0] >= 1.3]
            shown = together or [x for x in name_lines if x[0] > 0] or name_lines
            for _, _, _, _, c, off, line in shown[:5]:
                result["snippets"].append(_item(ev, c, off, extract._clean(line),
                                                why="about " + ", ".join(names.values())))
            # only lines that answer the whole question can show that the notes disagree
            facts = [((c, off, line), line, c.date) for _, _, full, _, c, off, line in together if full]
            together = [x for x in together if x[2]] or together
            covered = set()
            for x in together:
                covered |= said(x[6])
            if together:
                verdict = "found" if others <= covered else "partial"
                missing = _as_typed(question, sorted(others - covered))
            elif name_lines:
                verdict = "partial"
                missing = _as_typed(question, sorted(others))
                result["not_together"] = {"names": list(names.values()), "others": missing}
            result["verdict"], result["missing"] = verdict, missing
            result["about"] = list(names.values())
        for h in keep:
            c = h["chunk"]
            scored = sorted(((len(said(l)), -off, off, l) for off, l in enumerate(c.lines)
                             if l.strip() and not l.lstrip().startswith("#")), reverse=True)
            picked = [s for s in scored if s[0] >= need][:2]
            if not picked and (h["coverage"] >= 0.6 or not h["matched"]):
                picked = scored[:1]              # the block as a whole fits, even if no single line does
            if not picked:
                continue
            for _, _, off, line in sorted(picked, key=lambda s: s[2]):
                result["snippets"].append(_item(ev, c, off, extract._clean(line), why=_why(h)))
            for off, line in enumerate(c.lines):
                if len(said(line)) >= need:
                    facts.append(((c, off, line), line, c.date))
            ev.cite(c, _why(h))
        names = set(stems(" ".join(extract.find_people(session.docs.values()))))
        for old, new, kind, old_v, new_v in extract.find_changes(facts, ignore=names, limit=2):
            result["changes"].append({
                "kind": kind, "old_value": old_v, "new_value": new_v,
                "old": _item(ev, old[0], old[1], extract._clean(old[2])),
                "new": _item(ev, new[0], new[1], extract._clean(new[2]))})
        result["changes"] = _unique_changes(result["changes"])
        result["snippets"].sort(key=lambda s: s["date"], reverse=True)

        extra = []
        if verdict == "none":
            what = ", ".join("“%s”" % m for m in missing) or "that"
            extra.append("I could not find this. Nothing I am allowed to see mentions %s." % what)
            if result["snippets"]:
                extra.append("The closest notes are shown below, but they do not answer the question.")
        elif result.get("not_together"):
            nt = result["not_together"]
            extra.append("Your notes mention %s, but never together with %s, so they do not answer this as asked. "
                         "Below is what they do say about %s." % (
                             " and ".join("“%s”" % n for n in nt["names"]),
                             " or ".join("“%s”" % o for o in nt["others"]) or "the rest of your question",
                             " and ".join(nt["names"])))
        elif verdict == "partial":
            if missing:
                extra.append("I found related notes, but nothing that mentions %s. Treat this as a lead, not an answer."
                             % ", ".join("“%s”" % m for m in missing))
            else:
                extra.append("These notes are related by meaning rather than exact words. Check they are what you meant.")
        if result["snippets"]:
            newest = max(s["date"] for s in result["snippets"])
            if verdict != "none" and (today - _d(newest)).days > OLD_NOTE_DAYS:
                extra.append("The newest note on this is %s. It may be out of date." % dates.ago(_d(newest), today))
            if any(s["handwriting"] for s in result["snippets"]) and verdict != "none":
                extra.append("Part of this was read from handwriting or a screenshot. Open the original to check.")
        result["honesty"] = _honesty(session, extra)
        return result


_ASKING_WORDS = set(stems("decide decided decision say said says tell told think thought happen happened mean "
                          "discuss discussed mention mentioned note noted write wrote talk talked know plan "
                          "planned agree agreed update latest status news current currently", keep_stop=True))


def _name_words(session):
    """Stems of words the notes write as names: mostly capitalised, in the middle of a line."""
    from .index import stem
    cap, low = {}, {}
    for d in session.docs.values():
        if d.status != "ready":
            continue
        for line in d.lines:
            for w in re.findall(r"[A-Za-z][a-z]+", line)[1:]:
                (cap if w[0].isupper() else low)[w.lower()] = (cap if w[0].isupper() else low).get(w.lower(), 0) + 1
    return {stem(w) for w, n in cap.items()
            if n >= 2 and n > 2 * low.get(w, 0) and w.capitalize() not in extract.NOT_NAMES and w not in STOPWORDS}


def _names_in(session, question, asked):
    """Names in a question (a project, vendor or person, like Corvid or Priya): {stem: word}.

    A word counts if it is capitalised in the middle of the question, or, when
    typed in lower case, if the notes mostly write it with a capital letter.
    """
    from .index import stem
    out = {}
    words = re.findall(r"[A-Za-z][A-Za-z'’]+", question)
    text = None
    for i, w in enumerate(words):
        base = re.split(r"['’]", w)[0]
        st = stem(base.lower())
        if st not in asked or base.lower() in STOPWORDS or base in extract.NOT_NAMES:
            continue
        if base[0].isupper() and i > 0:
            out[st] = base
            continue
        if text is None:
            text = "\n".join(l for d in session.docs.values() for l in d.lines)
        cap = len(re.findall(r"\b%s\b" % re.escape(base.capitalize()), text))
        low = len(re.findall(r"\b%s\b" % re.escape(base.lower()), text))
        if cap >= 2 and cap > 2 * low:
            out[st] = base.capitalize()
    return out


def _d(iso):
    import datetime as dt
    return dt.date.fromisoformat(iso)


# ---- the local model's written layer ---------------------------------------
# How much of your notes goes to the model. Less text means it starts writing sooner;
# these are enough for a meeting brief because the best matches come first.
PREP_EXCERPTS, ASK_EXCERPTS, EXCERPT_CHARS = 10, 6, 450


def _focus(text, terms, width):
    """The part of a note most worth showing the model, at most `width` characters.

    Short notes go whole. Longer ones (Word and PDF files often have long
    paragraphs) are cut around the lines that mention what you asked about,
    so the line that answers the question is never the part left out.
    """
    if len(text) <= width:
        return text
    lines = text.split("\n")
    scores = [len(terms & set(stems(l))) for l in lines]
    if not any(scores):
        return text[:width].rstrip() + " …"
    best = max(range(len(lines)), key=lambda i: (scores[i], -i))
    line = lines[best]
    if len(line) >= width:                       # one long paragraph: a window around the matching words
        words = line.split(" ")
        hits = [i for i, w in enumerate(words) if terms & set(stems(w))] or [0]
        centre = hits[len(hits) // 2]
        lo = hi = centre
        size = len(words[centre])
        while (lo > 0 or hi < len(words) - 1) and size < width:
            if lo > 0:
                lo -= 1
                size += len(words[lo]) + 1
            if hi < len(words) - 1 and size < width:
                hi += 1
                size += len(words[hi]) + 1
        return ("… " if lo > 0 else "") + " ".join(words[lo:hi + 1]) + (" …" if hi < len(words) - 1 else "")
    keep, size = {best}, len(line)
    lo, hi = best, best
    while size < width and (lo > 0 or hi < len(lines) - 1):
        # grow towards whichever neighbour also mentions the subject, else downwards first
        up = scores[lo - 1] if lo > 0 else -1
        down = scores[hi + 1] if hi < len(lines) - 1 else -1
        i = hi + 1 if down >= up else lo - 1
        if size + len(lines[i]) + 1 > width:
            break
        keep.add(i)
        size += len(lines[i]) + 1
        lo, hi = min(lo, i), max(hi, i)
    return ("… " if lo > 0 else "") + "\n".join(lines[lo:hi + 1]) + (" …" if hi < len(lines) - 1 else "")


def _excerpts(evidence, terms=(), limit=99, width=EXCERPT_CHARS):
    terms = set(terms)
    parts = []
    for e in evidence[:limit]:
        kind = "typed note" if e["kind"] == "text" else "read from %s, may contain misreadings" % (e["image_kind"] or "an image")
        parts.append("[%d] %s | dated %s | %s\n%s" % (e["n"], e["doc"], e["date_label"], kind, _focus(e["text"], terms, width)))
    return "\n\n".join(parts)


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _ground(points, evidence, limit):
    """Keep only points the cited excerpts actually support. Returns (kept, dropped).

    A point is removed if it cites nothing, if too few of its words appear in
    the notes it cites, or if it contains a number that appears in none of
    the notes at all (an invented figure is the most dangerous mistake).
    """
    by_n = {e["n"]: e for e in evidence}

    def flat(items):
        return " ".join("%s %s %s %s" % (e["text"], e["date_label"], e["title"], e["date"]) for e in items).replace(",", "")
    everything = flat(evidence)
    kept, dropped = [], 0
    for p in _as_points(points):
        text, sources = p["text"], [n for n in dict.fromkeys(p["sources"]) if n in by_n]
        if text and not sources:
            sources = _find_sources(text, evidence)   # the model forgot to say: look for itself
        if not text or not sources:
            dropped += 1                           # no source, no say
            continue
        cited = flat(by_n[n] for n in sources)
        have = set(stems(cited, keep_stop=True))
        want = extract.content_stems(text)
        matched = len(want & have)
        support = matched / max(1, len(want))
        numbers = [n.replace(",", "").rstrip(".") for n in _NUM.findall(text)]
        if support < 0.5 or matched < min(2, len(want)) or any(n not in everything for n in numbers):
            dropped += 1
            continue
        numbers_ok = all(n in cited for n in numbers)
        good = support >= 0.7 and numbers_ok
        kept.append({"text": text, "sources": sources, "check": "ok" if good else "verify",
                     "why_verify": "" if good else (
                         "a number in it comes from a different note than the one cited" if not numbers_ok
                         else "worded more loosely than the cited note")})
        if len(kept) >= limit:
            break
    return kept, dropped


def _as_points(items):
    """The model's points, whatever shape a small model wrote them in."""
    if isinstance(items, (str, dict)):
        items = [items]
    out = []
    for p in items if isinstance(items, list) else []:
        if isinstance(p, str):
            out.append({"text": p.strip(), "sources": []})
        elif isinstance(p, dict):
            text = next((p[k] for k in ("point", "text", "answer", "summary", "content") if isinstance(p.get(k), str)), "")
            src = next((p[k] for k in ("sources", "source", "citations", "refs", "excerpts") if k in p), None)
            out.append({"text": text.strip(), "sources": _numbers(src)})
    return out


def _find_sources(text, evidence):
    """Which excerpts a point came from, when the model did not say: the one or two that
    contain most of its words (and all of its numbers)."""
    want = extract.content_stems(text)
    numbers = [n.replace(",", "").rstrip(".") for n in _NUM.findall(text)]
    scored = []
    for e in evidence:
        body = "%s %s %s" % (e["text"], e["date_label"], e["date"])
        have = set(stems(body, keep_stop=True))
        support = len(want & have) / max(1, len(want))
        if support >= 0.6 and all(n in body.replace(",", "") for n in numbers):
            scored.append((support, e["n"]))
    scored.sort(reverse=True)
    return [n for _, n in scored[:2]]


def _numbers(v):
    """Source numbers however a small model writes them: 2, "2", "[2]", "2, 3", [2, "3"], 2.0."""
    if isinstance(v, bool) or v is None:
        return []
    if isinstance(v, (int, float)):
        return [int(v)]
    if isinstance(v, str):
        return [int(x) for x in re.findall(r"\d+", v)]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(_numbers(x))
        return out
    return []


def _unique_changes(changes):
    """Two old notes saying £40k and two new ones saying £28k is one change, not two."""
    seen, out = set(), []
    for c in changes:
        key = (c["kind"], c["old_value"], c["new_value"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _model_or_reason(session):
    status = session.model.refresh(force=True)
    if not status["running"]:
        return None, ("The local model is not running, so there is no written summary. "
                      "Everything above still works. Start Ollama to add it.")
    if not status["chat"]:
        return None, "Ollama is running but no local model is installed. See the README for the one command to add one."
    return status["chat"], None


# Each written answer comes as a stream of events, so the page can show it
# while the local model is still writing:
#   {"type": "start", "model": ..., "excerpts_sent": n}
#   {"type": "partial", "draft": {...}}   unchecked, as far as it is written
#   {"type": "final", ...}                checked against the notes; this is the answer
# prep_ai / ask_ai give just the final event, for anything that does not stream.

def _clean_points(items):
    return [p for p in _as_points(items) if p["text"]]


def _final(result):
    out = dict(result)
    out["type"] = "final"
    return out


def _stream(session, model, system, user, schema, purpose, used, draft_of, finish, max_chars):
    yield {"type": "start", "model": model, "excerpts_sent": len(used)}
    # Tell the page what the wait is for, so a slow start is not a mystery.
    yield {"type": "status", "loading": not session.model.loaded(model),
           "waiting_for_image": session.reading_now is not None}
    out = None
    session.foreground_begin()                  # background image reading waits until this is done
    try:
        for kind, obj in session.model.stream_json(model, system, user, schema, purpose=purpose,
                                                   max_chars=max_chars):
            if kind == "thinking":
                yield {"type": "status", "thinking": True}
            elif kind == "partial":
                yield {"type": "partial", "draft": draft_of(obj)}
            else:
                out = obj
    except llm.ModelError as e:
        yield _final({"available": False, "reason": "The local model could not answer (%s)." % e})
        return
    finally:
        session.foreground_end()
    yield _final(finish(out if isinstance(out, dict) else {}))


def prep_ai_events(session, query):
    base = prep(session, query)
    if not base["found"] or not base["evidence"]:
        yield _final({"available": False, "reason": "There is nothing in your notes for me to summarise."})
        return
    model, reason = _model_or_reason(session)
    if not model:
        yield _final({"available": False, "reason": reason})
        return
    used = base["evidence"][:PREP_EXCERPTS]
    user = "Today is %s.\nThe meeting is about: %s\n\nExcerpts from the manager's notes:\n\n%s" % (
        dates.human(session.today), base["query"], _excerpts(used, base["terms"]))

    def draft_of(o):
        return {"headline": o.get("headline") if isinstance(o.get("headline"), str) else "",
                "talking_points": _clean_points(o.get("talking_points")),
                "watch_outs": _clean_points(o.get("watch_outs")),
                "gaps": [g for g in (o.get("gaps") or []) if isinstance(g, str) and g.strip()]}

    def finish(out):
        points, d1 = _ground(out.get("talking_points"), used, 3)
        watch, d2 = _ground(out.get("watch_outs"), used, 2)
        head, _ = _ground([{"point": out.get("headline"), "sources": [e["n"] for e in used]}], used, 1)
        return {"available": True, "model": model, "headline": head[0]["text"] if head else "",
                "talking_points": points, "watch_outs": watch,
                "gaps": [str(g) for g in (out.get("gaps") or [])][:2],
                "dropped": d1 + d2, "excerpts_sent": len(used)}

    system = llm.PREP_SYSTEM_SHORT if llm.small(session.model._sizes, model) else llm.PREP_SYSTEM
    for event in _stream(session, model, system, user, llm.PREP_SCHEMA,
                         "write meeting brief: %s" % base["query"][:60], used, draft_of, finish, 2600):
        yield event


def ask_ai_events(session, question):
    base = ask(session, question)
    if base["verdict"] == "none" or not base["evidence"]:
        yield _final({"available": False, "reason": "I did not ask the model, because there is nothing in your "
                                                    "notes for it to answer from. Better no answer than a made-up one."})
        return
    if base.get("not_together"):
        nt = base["not_together"]
        yield _final({"available": False, "reason": "No answer: I did not ask the model, because nothing in your "
                      "notes connects %s with %s. Better no answer than a made-up one." % (
                          " and ".join(nt["names"]), " or ".join("“%s”" % o for o in nt["others"]) or "the rest")})
        return
    model, reason = _model_or_reason(session)
    if not model:
        yield _final({"available": False, "reason": reason})
        return
    used = base["evidence"][:ASK_EXCERPTS]
    asked = base["question"]
    # A bare name or topic ("reinart", "Helix budget") is not a question; small models need it to be one.
    if "?" not in asked and len(asked.split()) <= 3:
        asked = "What do the notes say about %s?" % asked
    user = "Today is %s.\nQuestion: %s\n\nExcerpts from the manager's notes:\n\n%s" % (
        dates.human(session.today), asked, _excerpts(used, base["terms"]))

    def draft_of(o):
        return {"found": o.get("found") is not False, "answer": _clean_points(o.get("answer")),
                "caveat": o.get("caveat") if isinstance(o.get("caveat"), str) else ""}

    def finish(out):
        points, dropped = _ground(out.get("answer"), used, 2)
        said_yes = out.get("found") not in (False, "false", "no", 0)
        # The notes decide, not the model's yes/no: points that check out against notes
        # that clearly match the question are an answer, even if the model was unsure.
        found = bool(points) and (said_yes or base["verdict"] == "found")
        caveat = str(out.get("caveat") or "")
        if not found and base["verdict"] == "found":
            caveat = ""                          # "not in the notes" is wrong when the notes plainly match
        return {"available": True, "model": model, "found": found, "answer": points,
                "notes_match": base["verdict"] == "found",
                "caveat": caveat, "dropped": dropped, "excerpts_sent": len(used)}

    system = llm.ASK_SYSTEM_SHORT if llm.small(session.model._sizes, model) else llm.ASK_SYSTEM
    for event in _stream(session, model, system, user, llm.ASK_SCHEMA,
                         "answer: %s" % base["question"][:60], used, draft_of, finish, 1400):
        yield event


def _last(events):
    final = None
    for e in events:
        if e["type"] == "final":
            final = e
    final = dict(final)
    final.pop("type")
    return final


def prep_ai(session, query):
    return _last(prep_ai_events(session, query))


def ask_ai(session, question):
    return _last(ask_ai_events(session, question))
