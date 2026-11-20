"""Date parsing for informal notes: "3 Nov", "03/11/2026", "by Fri", "EOW"."""
from __future__ import annotations

import datetime as dt
import re

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
WEEKDAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}

_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ISO = re.compile(r"(?<!\d)(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)")
_NUM = re.compile(r"(?<![\d/])(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})(?![\d/])")
_NUM_SHORT = re.compile(r"\b(?:by|due|on|until|before|from|w/c)\s+(\d{1,2})/(\d{1,2})(?![\d/])", re.I)
_DMY = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?:,?\s+(20\d{2}))?", re.I)
_MDY = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(20\d{2}))?", re.I)
_WEEKDAY = re.compile(
    r"\b(by|on|due|before|until|next|this)\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
_RELATIVE = re.compile(r"\b(today|tomorrow|tmrw|eod|eow|end of (?:the )?week|next week|end of (?:the )?month)\b", re.I)


def _mk(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def _closest_year(month, day, ref):
    """Resolve a date without a year to the year nearest `ref`."""
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        d = _mk(y, month, day)
        if d and (best is None or abs((d - ref).days) < abs((best - ref).days)):
            best = d
    return best


def find_dates(text, ref=None, relative=True):
    """Return [{date, start, end, text, relative}] found in `text`.

    `ref` (the note's date) resolves missing years and relative phrases.
    """
    ref = ref or dt.date.today()
    found = []

    def add(d, m, rel=False):
        if d is None:
            return
        for f in found:                         # no overlapping matches
            if m.start() < f["end"] and m.end() > f["start"]:
                return
        found.append({"date": d, "start": m.start(), "end": m.end(),
                      "text": m.group(0), "relative": rel})

    for m in _ISO.finditer(text):
        add(_mk(m.group(1), m.group(2), m.group(3)), m)
    for m in _NUM.finditer(text):
        y = int(m.group(3))
        add(_mk(y + 2000 if y < 100 else y, m.group(2), m.group(1)), m)   # UK: day/month
    for m in _DMY.finditer(text):
        mon = MONTHS[m.group(2)[:3].lower()]
        add(_mk(m.group(3), mon, m.group(1)) if m.group(3) else _closest_year(mon, int(m.group(1)), ref), m)
    for m in _MDY.finditer(text):
        mon = MONTHS[m.group(1)[:3].lower()]
        add(_mk(m.group(3), mon, m.group(2)) if m.group(3) else _closest_year(mon, int(m.group(2)), ref), m)
    for m in _NUM_SHORT.finditer(text):
        add(_closest_year(int(m.group(2)), int(m.group(1)), ref), m)
    if relative:
        for m in _WEEKDAY.finditer(text):
            target = WEEKDAYS[m.group(2)[:3].lower()]
            ahead = (target - ref.weekday()) % 7
            if m.group(1).lower() == "next" and ahead < 3:
                ahead += 7
            add(ref + dt.timedelta(days=ahead), m, True)
        for m in _RELATIVE.finditer(text):
            w = m.group(1).lower()
            if w in ("today", "eod"):
                d = ref
            elif w in ("tomorrow", "tmrw"):
                d = ref + dt.timedelta(days=1)
            elif w == "next week":
                d = ref + dt.timedelta(days=7 - ref.weekday())
            elif "month" in w:
                nxt = (ref.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
                d = nxt - dt.timedelta(days=1)
            else:                                # end of week
                d = ref + dt.timedelta(days=(4 - ref.weekday()) % 7)
            add(d, m, True)
    found.sort(key=lambda f: f["start"])
    return found


def note_date(filename, lines, mtime):
    """Best-guess date for a note and its source: file name, first lines or mtime."""
    ref = dt.date.fromtimestamp(mtime)
    hits = find_dates(filename.replace("_", " "), ref, relative=False)
    if hits:
        return hits[0]["date"], "file name"
    for line in lines[:6]:
        hits = find_dates(line, ref, relative=False)
        if hits:
            return hits[0]["date"], "first lines of the note"
    return ref, "file's last-modified time"


def section_date(line, ref):
    """Date of a dated heading in a running note ("## 14 Oct"), else None."""
    bare = line.strip().lstrip("#*=-> ").strip()
    if not bare or len(bare) > 48:
        return None
    hits = find_dates(bare, ref, relative=False)
    if not hits:
        return None
    if hits[0]["start"] <= 6 or line.lstrip().startswith("#"):
        return hits[0]["date"]
    return None


def human(d, today=None):
    if d is None:
        return "undated"
    s = "%d %s" % (d.day, d.strftime("%b"))
    if today is None or d.year != today.year:
        s += " %d" % d.year
    return s


def ago(d, today):
    n = (today - d).days
    if n < 0:
        return "in %d day%s" % (-n, "" if n == -1 else "s")
    if n == 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 14:
        return "%d days ago" % n
    if n < 60:
        return "%d weeks ago" % (n // 7)
    return "%d months ago" % (n // 30)
