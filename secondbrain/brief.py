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
from .index import stems
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
                  "changes": [], "questions": [], "recent": [], "evidence": ev.items, "honesty": [],
                  "notes_used": 0, "found": False}
        if not terms:
            result["honesty"] = ["Tell me who or what the meeting is about."]
            return result

        hits = _retrieve(session, query, qv)
        relevant = [h for h in hits if h["coverage"] >= 0.5 or (h["sim"] or 0) >= 0.6]
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
                  "evidence": ev.items, "honesty": []}
        q, typo = idx.expand(question)
        terms = set(q) | set(typo)
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
        top_sim = max([h["sim"] or 0 for h in hits] or [0])
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

        keep = [h for h in hits if h["coverage"] >= 0.34 or (h["sim"] or 0) >= 0.5][:6]
        asked = set(q)

        def said(line):                              # which of the asked-about words a line covers
            got = set(stems(line))
            return {t for t in asked if t in got} | {typo[t] for t in typo if t in got}

        need = max(1, (len(asked) + 1) // 2)
        facts = []
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


def _d(iso):
    import datetime as dt
    return dt.date.fromisoformat(iso)


# ---- the local model's written layer ---------------------------------------
def _excerpts(evidence, limit=14, width=700):
    parts = []
    for e in evidence[:limit]:
        kind = "typed note" if e["kind"] == "text" else "read from %s, may contain misreadings" % (e["image_kind"] or "an image")
        parts.append("[%d] %s | dated %s | %s\n%s" % (e["n"], e["doc"], e["date_label"], kind, e["text"][:width]))
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
    for p in points or []:
        text = str(p.get("point") or "").strip()
        sources = [n for n in dict.fromkeys(p.get("sources") or []) if isinstance(n, int) and n in by_n]
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
    out = []
    for p in items if isinstance(items, list) else []:
        if isinstance(p, dict) and isinstance(p.get("point"), str) and p["point"].strip():
            out.append({"text": p["point"],
                        "sources": [x for x in (p.get("sources") or []) if isinstance(x, int)]})
    return out


def _final(result):
    out = dict(result)
    out["type"] = "final"
    return out


def _stream(session, model, system, user, schema, purpose, used, draft_of, finish):
    yield {"type": "start", "model": model, "excerpts_sent": len(used)}
    out = None
    try:
        for kind, obj in session.model.stream_json(model, system, user, schema, purpose=purpose):
            if kind == "partial":
                yield {"type": "partial", "draft": draft_of(obj)}
            else:
                out = obj
    except llm.ModelError as e:
        yield _final({"available": False, "reason": "The local model could not answer (%s)." % e})
        return
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
    used = base["evidence"][:14]
    user = "Today is %s.\nThe meeting is about: %s\n\nExcerpts from the manager's notes:\n\n%s" % (
        dates.human(session.today), base["query"], _excerpts(base["evidence"]))

    def draft_of(o):
        return {"headline": o.get("headline") if isinstance(o.get("headline"), str) else "",
                "talking_points": _clean_points(o.get("talking_points")),
                "watch_outs": _clean_points(o.get("watch_outs")),
                "gaps": [g for g in (o.get("gaps") or []) if isinstance(g, str) and g.strip()]}

    def finish(out):
        points, d1 = _ground(out.get("talking_points"), used, 5)
        watch, d2 = _ground(out.get("watch_outs"), used, 3)
        head, _ = _ground([{"point": out.get("headline"), "sources": [e["n"] for e in used]}], used, 1)
        return {"available": True, "model": model, "headline": head[0]["text"] if head else "",
                "talking_points": points, "watch_outs": watch,
                "gaps": [str(g) for g in (out.get("gaps") or [])][:3],
                "dropped": d1 + d2, "excerpts_sent": len(used)}

    for event in _stream(session, model, llm.PREP_SYSTEM, user, llm.PREP_SCHEMA,
                         "write meeting brief: %s" % base["query"][:60], used, draft_of, finish):
        yield event


def ask_ai_events(session, question):
    base = ask(session, question)
    if base["verdict"] == "none" or not base["evidence"]:
        yield _final({"available": False, "reason": "I did not ask the model, because there is nothing in your "
                                                    "notes for it to answer from. Better no answer than a made-up one."})
        return
    model, reason = _model_or_reason(session)
    if not model:
        yield _final({"available": False, "reason": reason})
        return
    used = base["evidence"][:8]
    user = "Today is %s.\nQuestion: %s\n\nExcerpts from the manager's notes:\n\n%s" % (
        dates.human(session.today), base["question"], _excerpts(base["evidence"], limit=8))

    def draft_of(o):
        return {"found": o.get("found") is not False, "answer": _clean_points(o.get("answer")),
                "caveat": o.get("caveat") if isinstance(o.get("caveat"), str) else ""}

    def finish(out):
        points, dropped = _ground(out.get("answer"), used, 3)
        found = bool(out.get("found")) and bool(points)
        return {"available": True, "model": model, "found": found, "answer": points if found else [],
                "caveat": str(out.get("caveat") or ""), "dropped": dropped, "excerpts_sent": len(used)}

    for event in _stream(session, model, llm.ASK_SYSTEM, user, llm.ASK_SCHEMA,
                         "answer: %s" % base["question"][:60], used, draft_of, finish):
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
