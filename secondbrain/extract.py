"""Pulling structure out of messy notes without a model.

Action items, decisions, open questions, who owes what, what looks done, and
where two notes disagree. These are plain pattern rules, so they are instant,
they work with no model installed, and every item points at one exact line of
one note. The local model (brief.py) adds a written summary on top; it does
not replace these.
"""
from __future__ import annotations

import re

from . import dates
from .index import stems, STOPWORDS

_BULLET = r"^\s*(?:[-*+>•]\s*)?"
_CHECKBOX = re.compile(_BULLET + r"\[(?P<mark>[ xX✓✔])\]\s*(?P<body>.+)$")
_LABEL = re.compile(
    _BULLET + r"(?:\*\*)?(?P<label>todo|to[- ]do|actions?(?: items?)?|ap|a/i|follow[- ]?ups?|next steps?|chase|reminder)"
    r"(?:\*\*)?\s*[:\-–—]+\s*(?:\*\*)?\s*(?P<body>\S.+)$", re.I)
_OWNER_TO = re.compile(
    _BULLET + r"@?(?P<owner>[A-Z][a-z]{1,14}|I|Me)\s+(?:to|will|needs? to|must|should|is going to)\s+(?P<body>[a-z].+)$")
_SELF = re.compile(r"\b(i need to|need to|remember to|don'?t forget(?: to)?|remind me to|i should|i must|i'll|i will|must|"
                   r"i said i'?d|i said i would|i promised|still haven'?t)\b", re.I)
_DONE_WORDS = r"done|sent|completed?|sorted|finished|delivered|received|shipped|signed|booked|closed|submitted"
_DONE = re.compile(r"(✓|✔|~~|\(done\)|\b(?:%s)\b\s*[.!]?\s*$|^\s*(?:[-*+]\s*)?done\b)" % _DONE_WORDS, re.I)
# "not done", "haven't signed off", "still waiting": the opposite of done or decided
_NEGATED = re.compile(r"\b(not|never|no|n't|nt|still waiting|not yet|yet to)\b[\w\s']{0,14}?\b(%s|signed[- ]off|sign[- ]off|"
                      r"approved|agreed|decided|confirmed)\b|\b(haven'?t|hasn'?t|didn'?t|wasn'?t|weren'?t|isn'?t|aren'?t)\b"
                      % "done|sent|completed?|sorted|finished|delivered|received|shipped|signed|booked|closed|submitted",
                      re.I)
_DONE_ANY = re.compile(r"(✓|✔|\[[xX]\]|\b(?:%s)\b)" % _DONE_WORDS, re.I)
_OWNER_MID = re.compile(r"(?:[.;,:]\s+|\s[-–—]\s)(?P<owner>[A-Z][a-z]{1,14})\s+to\s+(?P<body>[a-z]+\s.+)$")
_DECISION = re.compile(
    r"\b(decided|decision|agreed|agreement|we(?:'ll| will) go with|going with|go with|signed[- ]off|sign[- ]off|"
    r"approved|confirmed|settled on|final call|green ?lit|green light|cut to|moved to|pushed (?:back )?to|now|"
    r"said yes|said no|in principle|ruled? (?:it |them )?out|rules (?:it |them )?out)\b", re.I)
_STRONG_DECISION = re.compile(
    r"\b(decided|decision|agreed|agreement|we(?:'ll| will) go with|going with|signed[- ]off|sign[- ]off|approved|"
    r"confirmed|settled on|final call|green ?lit|green light|said yes|said no|in principle|ruled? (?:it |them )?out|"
    r"rules (?:it |them )?out)\b", re.I)
_UNDECIDED = re.compile(r"\b(not (?:yet )?(?:decided|agreed|confirmed)|no decision|undecided|tbc|tbd)\b", re.I)
_QUESTION = re.compile(
    r"(\?\s*$|\?\?|\b(tbc|tbd|unclear|not sure|unsure|need to check|check w/|check with|open question|unresolved|"
    r"don'?t know|no idea|find out)\b)", re.I)
_MONEY = re.compile(r"[£$€]\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m|bn)?\b", re.I)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_COUNT = re.compile(r"\b(\d+(?:\.\d+)?)\s?(fte|heads?|people|weeks?|days?|months?|licen[cs]es|seats|users|sprints?)\b", re.I)
_PEOPLE_LINE = re.compile(r"^\s*(?:[-*#]\s*)*(?:attendees|present|with|who|people|invited)\s*[:\-–]\s*(.+)$", re.I)
_ONE_TO_ONE = re.compile(r"\b1\s?[:\-–]\s?1\s+(?:with|w/)\s+([A-Z][a-z]+)|\b1on1\s+(?:with\s+)?([A-Z][a-z]+)", re.I)
_MENTION = re.compile(r"@([A-Z][a-z]+)")
_NAME = re.compile(r"^[A-Z][a-z]{1,14}$")

NOT_NAMES = set("""Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June
July August September October November December Mon Tue Tues Wed Thu Thur Thurs Fri Sat Sun Jan Feb Mar Apr Jun Jul
Aug Sep Sept Oct Nov Dec Action Actions Todo Notes Note Decision Decisions Agenda Update Updates Team Weekly Meeting
Next Done Open Risks Risk Me We They It He She The This That What When Where Why How Who Also But And Or If So Need
Check Draft Budget Finance Legal Hr Procurement Everyone All Nobody Someone Re Fwd Fyi Tbc Tbd Asap Eow Eod Ok Yes No
Not Still Maybe Probably Will Should Must Can Could Would Send Ask Chase Call Email Book Get Remember Follow Link
Links Idea Ideas Misc Random Stuff Private Sent Agreed Decided Moved Pilot Review Vendor Steering Scratch Photos
Whiteboard Screenshot Remind Ping Tell Confirm Sort Fix Write Read Print Share Bring""".split())


def _clean(text):
    text = re.sub(r"^\s*(?:(?:->|[-*+>•])\s*)?(?:\[[ xX✓✔]\]\s*)?", "", text)
    return re.sub(r"\s+", " ", text.replace("**", "").replace("~~", "")).strip()


def classify_line(line, ref_date):
    """Return a dict describing what kind of thing this line is, or None."""
    raw = line.rstrip()
    if not raw.strip() or raw.lstrip().startswith("#"):
        return None
    text = _clean(raw)
    if len(text) < 4:
        return None
    item = {"text": text, "kinds": []}

    action, owner, body = False, None, text
    m = _CHECKBOX.match(raw)
    if m:
        action, body = True, _clean(m.group("body"))
        item["done"] = m.group("mark") != " "
    else:
        m = _LABEL.match(raw)
        if m:
            action, body = True, _clean(m.group("body"))
        else:
            m = _OWNER_TO.match(raw)
            if m and len(raw) < 160 and m.group("owner") not in NOT_NAMES - {"Me"}:
                action, body = True, _clean(m.group("body"))
                owner = m.group("owner")
            else:
                m = _OWNER_MID.search(text)
                if m and len(text) < 200 and m.group("owner") not in NOT_NAMES:
                    action, owner, body = True, m.group("owner"), m.group("owner") + " to " + m.group("body")
                elif _SELF.search(text) and len(text) < 160 and not text.endswith("?"):
                    action, owner = True, "you"
    if action:
        m2 = re.match(r"@?([A-Z][a-z]{1,14})\s*(?:[-–—:]|\s(?:to|will)\b)\s*", body)
        if m2 and m2.group(1) not in NOT_NAMES:
            owner = m2.group(1)
        m2 = _MENTION.search(body) or re.search(r"\(([A-Z][a-z]{1,14})\)\s*$", body)
        if not owner and m2 and m2.group(1) not in NOT_NAMES:
            owner = m2.group(1)
        if owner in ("I", "Me"):
            owner = "you"
        item["kinds"].append("action")
        item["owner"] = owner
        item.setdefault("done", False)
        if _DONE.search(raw):
            item["done"] = True
        due = [d for d in dates.find_dates(text, ref_date)
               if re.search(r"\b(by|due|before|until|on|eow|eod|end of)\b", text[max(0, d["start"] - 12): d["end"]], re.I)
               or d["relative"]]
        if due:
            item["due"] = due[-1]["date"]

    if _UNDECIDED.search(text) or (_QUESTION.search(text) and not action):
        item["kinds"].append("question")
    elif _DECISION.search(text) and not text.endswith("?") and not _NEGATED.search(text):
        if _STRONG_DECISION.search(text) or values(text, ref_date):
            item["kinds"].append("decision")
    if not action and _DONE_ANY.search(raw) and not _NEGATED.search(raw):
        item["reports_done"] = True
    return item if (item["kinds"] or item.get("reports_done")) else None


def values(text, ref_date=None):
    """Numbers that matter in a line: money, percentages, counts, dates."""
    out = []
    for m in _MONEY.finditer(text):
        n = float(m.group(1).replace(",", ""))
        n *= {"k": 1e3, "m": 1e6, "bn": 1e9}.get((m.group(2) or "").lower(), 1)
        out.append(("money", n, m.group(0).strip()))
    for m in _PERCENT.finditer(text):
        out.append(("percent", float(m.group(1)), m.group(0)))
    for m in _COUNT.finditer(text):
        out.append(("count:" + m.group(2).lower().rstrip("s"), float(m.group(1)), m.group(0)))
    if ref_date is not None:
        for d in dates.find_dates(text, ref_date, relative=False):
            out.append(("date", d["date"].toordinal(), d["text"]))
    return out


def content_stems(text, ignore=()):
    return {s for s in stems(text) if len(s) > 2 and not s[0].isdigit() and s not in ignore}


def find_changes(lines, ignore=(), limit=4):
    """Where an older note and a newer note give different numbers for the same thing.

    `lines` is [(ref, text, date)] where ref is anything the caller wants back.
    Returns [(older_ref, newer_ref, kind, old_value, new_value)].
    """
    facts = []
    for ref, text, date in lines:
        vals = values(text, date)
        if vals:
            facts.append((ref, date, content_stems(text, ignore) - _MONTH_STEMS, vals))
    found = []
    for i, (ra, da, sa, va) in enumerate(facts):
        for rb, db, sb, vb in facts[i + 1:]:
            if da == db:
                continue
            older, newer = ((ra, sa, va), (rb, sb, vb)) if da < db else ((rb, sb, vb), (ra, sa, va))
            shared = older[1] & newer[1]
            for kind, old, old_text in older[2]:
                need = 3 if kind == "date" else 2
                if len(shared) < need:
                    continue
                new = [v for v in newer[2] if v[0] == kind]
                if new and kind != "date" and not (0.2 <= (new[0][1] or 1) / (old or 1) <= 5):
                    continue                     # too different to be the same quantity revised
                if new and all(v[1] != old for v in new) and not any(
                        v[0] == kind and v[1] == new[0][1] for v in older[2]):
                    found.append((len(shared), older[0], newer[0], kind.split(":")[0], old_text, new[0][2]))
                    break
    found.sort(key=lambda f: -f[0])
    out, seen = [], set()
    for _, o, n, kind, ov, nv in found:
        if id(o) in seen or (id(n), kind) in seen:
            continue
        seen.add(id(o))
        seen.add((id(n), kind))
        out.append((o, n, kind, ov, nv))
        if len(out) >= limit:
            break
    return out


_MONTH_STEMS = set(stems("jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june "
                         "july august september october november december mon tue tues wed thu thurs fri"))


_VERB_STEMS = set(stems("send sent do done complete completed sort sorted finish finished deliver delivered receive "
                        "received ship shipped sign signed book booked close closed submit submitted share shared "
                        "draft write pull together get chase check ask tell"))


def resolves(action_text, later_text, owner=None):
    """Does a later line look like it reports this action as done?"""
    if not _DONE_ANY.search(later_text) or _NEGATED.search(later_text):
        return False
    a = content_stems(action_text) - _MONTH_STEMS - _VERB_STEMS
    b = content_stems(later_text) - _MONTH_STEMS - _VERB_STEMS
    shared = a & b
    if owner and owner.lower() in shared:
        return len(shared) >= 2
    return len(shared) >= 2 and len(shared) >= 0.34 * max(1, min(len(a), 6))


def find_people(docs, limit=8):
    """Names that keep turning up in the notes (for the suggestion chips)."""
    candidates = {}
    text_all = []
    for doc in docs:
        text_all.append(doc.title)
        for name in re.findall(r"[A-Za-z]+", doc.title):
            if _NAME.match(name.capitalize()) and len(name) > 2:
                candidates.setdefault(name.capitalize(), 0)
        for line in doc.lines:
            text_all.append(line)
            m = _PEOPLE_LINE.match(line)
            if m:
                for part in re.split(r"[,/&+;]|\band\b", m.group(1)):
                    words = part.strip().split()
                    inside = re.findall(r"\(([A-Z][a-z]{1,14})\)", part)
                    if inside:                       # "Helix account manager (Rina)": the person is Rina
                        first = inside[0]
                    elif len(words) <= 2:            # "Tom" or "Mei Lin"
                        first = words[0].strip("()@.") if words else ""
                    else:                            # a description, not a name
                        continue
                    if _NAME.match(first) and first not in NOT_NAMES:
                        candidates[first] = candidates.get(first, 0) + 3
            for m in _ONE_TO_ONE.finditer(line):
                n = (m.group(1) or m.group(2) or "").capitalize()
                if n and n not in NOT_NAMES:
                    candidates[n] = candidates.get(n, 0) + 3
            for n in _MENTION.findall(line):
                if n not in NOT_NAMES:
                    candidates[n] = candidates.get(n, 0) + 2
            m = _OWNER_TO.match(line)
            # "Tom to send…" is a person; "Helix will hold the price" could be a company
            if m and re.match(r"\s*(?:[-*+>•]\s*)?@?%s\s+to\s" % m.group("owner"), line) and \
                    m.group("owner") not in NOT_NAMES and m.group("owner") not in ("I", "Me"):
                candidates[m.group("owner")] = candidates.get(m.group("owner"), 0) + 2
    blob = "\n".join(text_all)
    scored = []
    for name, seed in candidates.items():
        if seed <= 0:
            continue                             # a word in a file name alone is not a person
        count = len(re.findall(r"\b%s\b" % re.escape(name), blob))
        scored.append((seed + count, name))
    scored.sort(reverse=True)
    return [n for _, n in scored[:limit]]


def find_topics(docs, people, limit=5):
    """Capitalised words used mid-sentence across several notes: project and vendor names."""
    counts, in_docs = {}, {}
    skip = NOT_NAMES | set(people)
    for doc in docs:
        for line in doc.lines:
            toks = re.findall(r"[A-Za-z][A-Za-z0-9]+", line)
            for w in toks[1:]:
                if w[0].isupper() and w[1:].islower() and len(w) > 3 and w not in skip and w.lower() not in STOPWORDS:
                    counts[w] = counts.get(w, 0) + 1
                    in_docs.setdefault(w, set()).add(doc.id)
    ranked = sorted(((c, w) for w, c in counts.items() if c >= 3 and len(in_docs[w]) >= 2), reverse=True)
    return [w for _, w in ranked[:limit]]
