/* Second Brain: the page. Talks only to the local server it was loaded from.
   Note text is always inserted as text (never as HTML), and nothing is put in
   browser storage: close the tab and the page has kept nothing either. */
"use strict";

// ---------- tiny helpers ----------
function h(tag, attrs) {
  const el = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "on") for (const ev in v) el.addEventListener(ev, v[ev]);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
  return el;
}
function add(el, child) {
  if (child == null || child === false) return;
  if (Array.isArray(child)) child.forEach(c => add(el, c));
  else el.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
}
const $ = id => document.getElementById(id);
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Second-Brain": "1" } };
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "Something went wrong");
  return data;
}
const plural = (n, one, many) => n + " " + (n === 1 ? one : (many || one + "s"));

// Must match __version__ in secondbrain/__init__.py.
const APP_VERSION = "0.4.1";
let STATE = null;
let EVIDENCE = [];          // excerpts behind whatever is on screen
let lastFolders = "", lastChips = "";

// ---------- left: what I can see ----------
const STATUS = { queued: "waiting to read", reading: "reading…", no_model: "can't read: no vision model",
                 failed: "couldn't read", empty: "no text found" };

function renderFolders(s) {
  const key = JSON.stringify(s.units);
  if (key === lastFolders) return;
  lastFolders = key;
  const box = clear($("folders"));
  s.units.forEach(u => {
    const meta = u.allowed
      ? plural(u.notes.length, "note") + " in memory" + (u.skipped ? ", " + u.skipped + " skipped (file type)" : "")
      : "off: not opened, not listed";
    const el = h("div", { class: "folder" + (u.allowed ? " on" : "") },
      h("div", { class: "folder-head" },
        h("button", { class: "switch" + (u.allowed ? " on" : ""), role: "switch", "aria-checked": String(u.allowed),
                      "aria-label": "Let the assistant see " + u.name,
                      on: { click: () => toggle(u.name, !u.allowed) } }),
        h("span", { class: "folder-name" }, u.name, h("span", { class: "folder-meta" }, meta))));
    if (u.allowed) {
      const list = h("ul", { class: "notes" });
      u.notes.forEach(n => list.appendChild(h("li", null,
        h("span", { class: "nm", title: "Open " + n.id, on: { click: () => openSource({ doc: n.id }) } }, n.name),
        n.kind === "image" && n.status === "ready"
          ? h("span", { class: "st" }, /\.pdf$/i.test(n.id) ? "scanned PDF" : n.image_kind === "screenshot" ? "screenshot" : "handwriting") : null,
        STATUS[n.status] ? h("span", { class: "st" + (n.status === "reading" || n.status === "queued" ? " busy" : ""),
          title: n.note || "" }, STATUS[n.status]) : null,
        h("button", { class: "hide", on: { click: () => hide(n.id, true) } }, "hide"))));
      u.hidden.forEach(id => list.appendChild(h("li", { class: "hidden" },
        h("span", { class: "nm" }, id.split("/").slice(1).join("/") || id),
        h("button", { class: "hide", on: { click: () => hide(id, false) } }, "show"))));
      el.appendChild(list);
    }
    box.appendChild(el);
  });
  $("folder-path").textContent = "Notes folder: " + s.folder;
}
async function toggle(folder, on) { apply(await api("/api/allow", { folder, on })); }
async function hide(note, hideIt) { apply(await api("/api/hide", { note, hide: hideIt })); }
$("all-on").onclick = async () => apply(await api("/api/allow_all", { on: true }));
$("all-off").onclick = async () => apply(await api("/api/allow_all", { on: false }));

// ---------- right: receipt ----------
function describe(e) {
  const d = e.detail || {};
  switch (e.kind) {
    case "read": return ["Read ", h("b", null, d.path)];
    case "allow": return ["You switched on ", h("b", null, d.folder || d.note)];
    case "revoke": return ["You switched off ", h("b", null, d.folder || d.note), ". Forgot " + plural(d.notes_forgotten || 0, "note") + "."];
    case "model": return [h("span", { class: "model" }, "Local model"), ": " + (d.purpose || "request") +
      ". " + (d.sent_chars || 0).toLocaleString() + " characters" + (d.images ? " and " + plural(d.images, "image") : "") +
      " passed to " + d.model + " on this device" + (d.error ? " (failed: " + d.error + ")" : "") + ".",
      d.reply ? h("details", { class: "reply" }, h("summary", null, "What it replied"), h("pre", null, d.reply)) : null];
    case "blocked": return [h("span", { class: "blocked" }, "Blocked"), ": tried to " + d.what + "."];
    case "selftest": return ["You tested the lock: " + d.blocked + " of " + d.attempts + " attempts blocked."];
    default: return [d.message || e.kind];
  }
}
function renderReceipt(s) {
  const r = s.receipt;
  $("c-read").textContent = r.files_read;
  $("c-sent").textContent = r.sent_off_device;
  $("c-saved").textContent = r.written_to_disk;
  $("mem").textContent = "In memory right now: " + plural(s.notes_in_memory, "note") + ", " +
    plural(s.chunks_in_memory, "search entry", "search entries") + ". " +
    (r.blocked ? plural(r.blocked, "attempt") + " to break the rule blocked. " : "") +
    "On disk: nothing.";
  const list = clear($("events"));
  if (!r.events.length) list.appendChild(h("li", null, "Nothing yet. I have not opened a single note."));
  r.events.slice().reverse().forEach(e => {
    const t = new Date(e.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    list.appendChild(h("li", null, h("time", null, t), describe(e)));
  });
}
function renderPills(s) {
  const lock = $("pill-lock");
  lock.className = s.locked ? "good" : "warn";
  lock.textContent = s.locked ? "Locked: this app cannot save or send anything." : "The lock is off.";
  const m = s.model, pill = $("pill-model");
  if (m.running && m.chat) {
    pill.className = "good";
    pill.textContent = "Model on this device: " + m.chat + (m.vision ? "." : " (cannot read images).");
  } else {
    pill.className = "warn";
    pill.textContent = m.running ? "No local model installed." : "No local model running, so no summaries or handwriting.";
  }
  pill.title = m.error || (m.cloud_models_ignored && m.cloud_models_ignored.length
    ? "Ignored cloud models: " + m.cloud_models_ignored.join(", ") : "");
}
function renderChips(s) {
  const key = JSON.stringify([s.people, s.topics, s.notes_in_memory > 0]);
  if (key === lastChips) return;
  lastChips = key;
  const pc = clear($("prep-chips")), ac = clear($("ask-chips"));
  const names = s.people.concat(s.topics);
  if (names.length) {
    pc.appendChild(h("span", { class: "lbl" }, "From your notes:"));
    names.forEach(n => pc.appendChild(h("button", { type: "button", class: "chip",
      on: { click: () => { $("prep-q").value = n; runPrep(); } } }, n)));
  }
  if (s.notes_in_memory) {
    ac.appendChild(h("span", { class: "lbl" }, "Try:"));
    const t = s.topics[0], p = s.people[0];
    [t && "What did we decide about the " + t + " budget?", t && "When does the " + t + " pilot start?",
     p && "What is " + p + "'s salary?"].filter(Boolean).forEach(q => ac.appendChild(
      h("button", { type: "button", class: "chip", on: { click: () => { $("ask-q").value = q; runAsk(); } } }, q)));
  }
}
function apply(s) {
  STATE = s;
  // The page is fresh from disk, but the app behind it may be an older copy still running.
  const stale = s.version !== APP_VERSION;
  $("stale").hidden = !stale;
  renderFolders(s); renderReceipt(s); renderPills(s); renderChips(s);
  ["prep-out", "ask-out"].forEach(id => {
    const out = $(id);
    if (!out.firstChild || out.firstChild.dataset.kind === "empty") renderEmpty(out, s);
  });
}
function renderEmpty(out, s) {
  clear(out);
  const blind = !s.units.some(u => u.allowed);
  out.appendChild(h("div", { class: "empty", "data-kind": "empty" },
    h("b", null, blind ? "I can't see anything yet." : "Ready when you are."),
    blind ? "Switch on the folders you want me to use, on the left. I only open what you switch on."
          : "I have " + plural(s.notes_in_memory, "note") + " in memory" + (s.reading ? " and am still reading " + plural(s.reading, "image") : "") + ". Nothing has been saved or sent anywhere."));
}
async function poll() {
  try { apply(await api("/api/state")); } catch (e) { /* server stopped */ }
  setTimeout(poll, 1500);
}

// ---------- shared rendering ----------
function cite(n) {
  return h("button", { type: "button", class: "cite", title: "Show the note this came from",
    on: { click: () => { const e = EVIDENCE.find(x => x.n === n); if (e) openSource(e); } } }, n);
}
function where(it) {
  return h("div", { class: "where" }, it.doc + ", line " + it.line + ", written " + it.ago,
    it.handwriting ? h("span", { class: "badge grey" }, "read from an image") : null,
    it.uncertain ? h("span", { class: "badge amber" }, "unsure of a word") : null);
}
// One line of a brief: the date it was written in the margin, the note's words beside it.
function row(it) {
  const body = h("div", { class: "what" });
  for (let i = 1; i < arguments.length; i++) add(body, arguments[i]);
  add(body, where(it));
  return h("li", { class: "line" }, h("div", { class: "when" }, it.date_label), body);
}
function honesty(list, title) {
  if (!list || !list.length) return null;
  return h("div", { class: "card honest" }, h("h3", null, title || "Before you rely on this"),
    h("ul", null, list.map(x => h("li", null, x))));
}
function section(title, sub, items, render) {
  if (!items || !items.length) return null;
  return h("div", { class: "card" }, h("h3", null, title), sub ? h("p", { class: "sub" }, sub) : null,
    h("ul", { class: "items" }, items.map(render)));
}
function changeCard(c) {
  return [
    h("li", { class: "line change-head" }, h("div", { class: "when" }),
      h("div", { class: "what change" }, h("span", { class: "struck" }, c.old_value), " is now ", h("mark", null, c.new_value))),
    row(c.new, c.new.text, cite(c.new.n)),
    row(c.old, h("span", { class: "was" }, c.old.text), cite(c.old.n))];
}
function points(list) {
  return h("ul", { class: "items" }, list.map(p => h("li", null, p.text, p.sources.map(cite),
    p.check === "verify" ? h("span", { class: "badge amber", title: p.why_verify }, "check the source") : null)));
}
// ---------- written answers, shown while the local model writes them ----------
const streams = {};
async function streamAI(key, path, body, onEvent) {
  if (streams[key]) streams[key].abort();          // a new question: stop the model writing the old answer
  const ctrl = new AbortController();
  streams[key] = ctrl;
  let finished = false;
  const handle = ev => { if (ev.type === "final") finished = true; onEvent(ev); };
  try {
    const r = await fetch(path, { method: "POST", body: JSON.stringify(body), signal: ctrl.signal,
      headers: { "Content-Type": "application/json", "X-Second-Brain": "1" } });
    if (!r.ok || !r.body) throw new Error("the app did not answer");
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (line) handle(JSON.parse(line));
      }
    }
    if (!finished) handle({ type: "final", available: false, reason: "The answer stopped part-way. Try again." });
  } catch (e) {
    if (e.name !== "AbortError") handle({ type: "final", available: false, reason: "The answer stopped part-way (" + e.message + ")." });
  } finally {
    if (streams[key] === ctrl) delete streams[key];
  }
}
function drafting(card, title, ev) {
  card.classList.add("drafting");
  clear(card).appendChild(h("h3", null, title));
  card.appendChild(h("p", { class: "sub" }, "Being written by " + ev.model + " on this device from " + plural(ev.excerpts_sent, "excerpt") +
    ". This is a draft: when it is finished I check every point against your notes and remove any I cannot trace."));
  card.appendChild(h("div", { class: "draft" }, h("p", { class: "loading", "data-since": String(Date.now()) }, "Starting…")));
}
function waiting(card, ev) {
  const body = card.querySelector(".draft");
  if (!body || body.querySelector(".headline, ul")) return;
  const since = (body.querySelector("[data-since]") || {}).dataset;
  const why = ev.thinking ? "The model is reasoning before it writes. This version of the model always does that, which is slow. " +
      "Installing one whose name ends in -instruct (see the README) makes answers start much sooner."
    : ev.loading ? "Loading the model into memory first. This happens once per session and can take up to a minute on a laptop."
    : ev.waiting_for_image ? "Waiting for the model to finish reading an image, then it will start on this."
    : "Reading your notes. The first words usually appear within a few seconds.";
  clear(body).appendChild(h("p", { class: "loading", "data-since": since ? since.since : String(Date.now()) }, why));
}
// A seconds counter on anything still waiting, so a slow laptop does not look frozen.
setInterval(() => document.querySelectorAll("[data-since]").forEach(el => {
  let t = el.querySelector(".secs");
  if (!t) { t = h("span", { class: "secs" }); el.appendChild(t); }
  t.textContent = " (" + Math.round((Date.now() - Number(el.dataset.since)) / 1000) + "s)";
}), 1000);
function draftBody(card) {
  let body = card.querySelector(".draft");
  if (!body) { body = h("div", { class: "draft" }); card.appendChild(body); }
  return clear(body);
}
function draftList(texts) {
  return h("ul", { class: "items" }, texts.map(t => h("li", null, t)));
}
function aiCard(title) {
  return h("div", { class: "card ai" }, h("h3", null, title), h("p", { class: "loading" }, "Writing this on your device. Nothing is being sent anywhere."));
}

// ---------- prepare for a meeting ----------
let prepRun = 0;
async function runPrep() {
  const q = $("prep-q").value.trim();
  if (!q) return;
  const run = ++prepRun, out = clear($("prep-out"));
  out.appendChild(h("p", { class: "loading" }, "Looking through what you have let me see…"));
  let b;
  try { b = await api("/api/prep", { query: q }); } catch (e) { clear(out).appendChild(h("div", { class: "card none" }, e.message)); return; }
  if (run !== prepRun) return;
  EVIDENCE = b.evidence;
  clear(out);
  out.appendChild(h("h2", { class: "title" }, "Before you meet: " + b.query));
  if (!b.found) {
    out.appendChild(h("div", { class: "card none" }, h("h3", null, "I have nothing on this."),
      h("p", { class: "sub" }, "I would rather say so than guess.")));
    add(out, honesty(b.honesty, "Why that might be"));
    return;
  }
  out.appendChild(h("p", { class: "meta" }, "Put together just now from " + plural(b.notes_used, "note") +
    ". Click a number to see the note it came from."));
  const ai = aiCard("In short");
  out.appendChild(ai);
  add(out, honesty(b.honesty));
  if (b.changes.length) out.appendChild(h("div", { class: "card changed" }, h("h3", null, "Changed since you first wrote it down"),
    h("p", { class: "sub" }, "Two of your notes disagree. The newer one is probably right."),
    h("ul", { class: "items" }, b.changes.map(changeCard))));
  add(out, section("Still open", "Promised or planned, and not marked done anywhere I can see.", b.open_actions, a => row(a,
    a.owner ? h("b", null, (a.owner === "you" ? "You" : a.owner) + ": ") : null, a.text, cite(a.n),
    a.overdue ? h("span", { class: "badge red" }, "was due " + a.due_label) : (a.due_label ? h("span", { class: "badge grey" }, "due " + a.due_label) : null),
    a.stale && !a.overdue ? h("span", { class: "badge amber" }, "is this still true?") : null)));
  add(out, section("Probably done or overtaken", "A later note looks like it closes or replaces these. Worth a quick check, not a chase.", b.probably_done, a => row(a,
    h("span", { class: "was" }, a.text), cite(a.n),
    h("div", { class: "sub-line" }, "On " + a.resolved_by.date_label + " you wrote: " + a.resolved_by.text, cite(a.resolved_by.n)))));
  add(out, section("Decisions on record", null, b.decisions, d => row(d,
    h("span", { class: d.superseded_by ? "was" : "" }, d.text), cite(d.n),
    d.superseded_by ? h("span", { class: "badge amber" }, "changed on " + d.superseded_by.date_label) : null)));
  add(out, section("Unresolved", "Questions and loose ends in your own words.", b.questions, x => row(x, x.text, cite(x.n))));
  add(out, section("Most recent mentions", null, b.recent, x => row(x, x.text, cite(x.n))));

  streamAI("prep", "/api/prep/ai/stream", { query: q }, ev => {
    if (run !== prepRun) return;
    if (ev.type === "start") return drafting(ai, "In short", ev);
    if (ev.type === "status") return waiting(ai, ev);
    if (ev.type === "partial") {
      const d = ev.draft, body = draftBody(ai);
      if (d.headline) body.appendChild(h("p", { class: "headline" }, d.headline));
      if (d.talking_points.length) add(body, [h("h4", null, "Raise"), draftList(d.talking_points.map(p => p.text))]);
      if (d.watch_outs.length) add(body, [h("h4", null, "Watch out for"), draftList(d.watch_outs.map(p => p.text))]);
      if (d.gaps.length) add(body, [h("h4", null, "Not in your notes"), draftList(d.gaps)]);
      return;
    }
    const s = ev;
    ai.classList.remove("drafting");
    clear(ai).appendChild(h("h3", null, "In short"));
    if (!s.available) { ai.className = "card"; ai.appendChild(h("p", { class: "sub" }, s.reason)); return; }
    ai.appendChild(h("p", { class: "sub" }, "Written by " + s.model + " on this device from " + plural(s.excerpts_sent, "excerpt") +
      ", then checked line by line against your notes."));
    if (s.headline) ai.appendChild(h("p", { class: "headline" }, s.headline));
    if (s.talking_points.length) add(ai, [h("h4", null, "Raise"), points(s.talking_points)]);
    if (s.watch_outs.length) add(ai, [h("h4", null, "Watch out for"), points(s.watch_outs)]);
    if (s.gaps.length) add(ai, [h("h4", null, "Not in your notes"), h("ul", { class: "items" }, s.gaps.map(g => h("li", null, g)))]);
    if (s.dropped) ai.appendChild(h("p", { class: "sub" }, "I removed " + plural(s.dropped, "point") +
      " the model wrote that I could not trace back to a note."));
    if (!s.talking_points.length && !s.watch_outs.length) ai.appendChild(h("p", { class: "sub" }, "The model produced nothing I could back up with your notes, so I am showing only the notes themselves."));
  });
}
$("prep-form").onsubmit = e => { e.preventDefault(); runPrep(); };

// ---------- find something ----------
let askRun = 0;
async function runAsk() {
  const q = $("ask-q").value.trim();
  if (!q) return;
  const run = ++askRun, out = clear($("ask-out"));
  out.appendChild(h("p", { class: "loading" }, "Looking through what you have let me see…"));
  let a;
  try { a = await api("/api/ask", { question: q }); } catch (e) { clear(out).appendChild(h("div", { class: "card none" }, e.message)); return; }
  if (run !== askRun) return;
  EVIDENCE = a.evidence;
  clear(out);
  out.appendChild(h("h2", { class: "title" }, a.question));
  let ai = null;
  if (a.verdict === "none") {
    out.appendChild(h("div", { class: "card none" }, h("h3", null, "That is not in the notes I can see."),
      h("p", { class: "sub" }, "I would rather say so than guess.")));
    add(out, honesty(a.honesty, "Why that might be"));
  } else {
    ai = aiCard("Answer");
    out.appendChild(ai);
    add(out, honesty(a.honesty));
  }
  if (a.changes.length) out.appendChild(h("div", { class: "card changed" }, h("h3", null, "Your notes disagree on this"),
    h("p", { class: "sub" }, "The newer one is probably right."), h("ul", { class: "items" }, a.changes.map(changeCard))));
  const notesTitle = a.verdict === "none" ? "Closest notes (these do not answer it)"
    : a.not_together ? "What your notes say about " + a.not_together.names.join(" and ")
    : "What your notes say";
  const notesCard = section(notesTitle, "Newest first.", a.snippets, s => row(s, s.text, cite(s.n)));
  add(out, notesCard);
  if (!ai) return;
  streamAI("ask", "/api/ask/ai/stream", { question: q }, ev => {
    if (run !== askRun) return;
    if (ev.type === "start") return drafting(ai, "Answer", ev);
    if (ev.type === "status") return waiting(ai, ev);
    if (ev.type === "partial") {
      const d = ev.draft, body = draftBody(ai);
      if (d.answer.length) body.appendChild(draftList(d.answer.map(p => p.text)));
      if (d.caveat) body.appendChild(h("p", { class: "sub" }, d.caveat));
      return;
    }
    const s = ev;
    ai.classList.remove("drafting");
    clear(ai).appendChild(h("h3", null, "Answer"));
    if (!s.available) { ai.className = "card"; ai.appendChild(h("p", { class: "sub" }, s.reason)); return; }
    ai.appendChild(h("p", { class: "sub" }, "Written by " + s.model + " on this device, then checked against your notes."));
    if (s.found) ai.appendChild(points(s.answer));
    else if (s.notes_match) {
      // the notes clearly match; the model just could not turn them into a checked answer
      ai.appendChild(h("p", { class: "sub" }, "The model did not write an answer I could check against your notes. " +
        "The lines below are what your notes say: click a number to read the note."));
    }
    else {
      ai.appendChild(h("p", { class: "headline" }, "Your notes do not answer this as asked."));
      if (s.answer.length) add(ai, [h("h4", null, "What they do say"), points(s.answer)]);
      // the notes listed below are related, not an answer: say so
      if (notesCard && notesCard.firstChild.textContent === "What your notes say")
        notesCard.firstChild.textContent = "Related notes (these do not answer it as asked)";
      out.querySelectorAll(".card.changed > h3").forEach(t => { t.textContent = "Related notes that disagree"; });
    }
    if (s.caveat) ai.appendChild(h("p", { class: "sub" }, s.caveat));
    if (s.dropped) ai.appendChild(h("p", { class: "sub" }, "I removed " + plural(s.dropped, "point") + " I could not trace back to a note."));
  });
}
$("ask-form").onsubmit = e => { e.preventDefault(); runAsk(); };

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => { x.classList.toggle("on", x === t); x.setAttribute("aria-selected", String(x === t)); });
  $("tab-prep").hidden = t.dataset.tab !== "prep";
  $("tab-ask").hidden = t.dataset.tab !== "ask";
});

// ---------- modal: source viewer, lock test, final receipt ----------
function openModal(content) { add(clear($("modal-body")), content); $("overlay").hidden = false; $("modal-close").focus(); }
function closeModal() { $("overlay").hidden = true; clear($("modal-body")); }
$("modal-close").onclick = closeModal;
$("overlay").onclick = e => { if (e.target === $("overlay")) closeModal(); };
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

async function openSource(e) {
  let doc;
  try { doc = await api("/api/source?id=" + encodeURIComponent(e.doc)); }
  catch (err) { openModal([h("h2", null, e.doc), h("p", null, err.message)]); return; }
  const src = h("div", { class: "src" });
  let first = null;
  doc.lines.forEach((line, i) => {
    const hit = e.start && i + 1 >= e.start && i + 1 <= e.end;
    const row = h("div", { class: "ln" + (hit ? " hit" : "") }, h("i", null, i + 1), line || " ");
    if (hit && !first) first = row;
    src.appendChild(row);
  });
  const isImage = doc.kind === "image";
  const info = [
    h("h2", null, doc.id),
    doc.status === "failed" || doc.status === "empty" ? h("p", { class: "card honest" }, "I could not read this note: " + (doc.note || "unknown reason") + ".") : null,
    h("p", { class: "meta" }, "Dated " + doc.date + " (from the " + doc.date_source + ")" +
      (e.why ? ". Shown because it " + e.why + "." : ".")),
    isImage ? h("p", { class: "card honest" }, "This is my reading of " + (/\.pdf$/i.test(doc.id) ? "a scanned PDF" : doc.image_kind === "screenshot" ? "a screenshot" : "handwriting") +
      ", done by the model on this device" + (doc.uncertain ? ". Words marked [?] are ones I was unsure of" : "") +
      ". The picture is the truth; my transcription is a guess. It exists only in memory.") : null,
    isImage ? h("div", { class: "split" },
      h("div", null, Array.from({ length: Math.max(1, doc.pages) }, (_, i) => h("img", {
        src: "/api/image?id=" + encodeURIComponent(doc.id) + "&page=" + (i + 1),
        alt: "Page " + (i + 1) + " of the original" }))), src) : src,
    h("p", { class: "path" }, "Fingerprint when I read it: " + doc.fingerprint + "…  The file itself has not been touched."),
  ];
  openModal(info);
  if (first) first.scrollIntoView({ block: "center" });
}

$("selftest").onclick = async () => {
  const r = await api("/api/selftest", {});
  apply(r.state);
  const all = r.results.every(x => x.blocked);
  openModal([
    h("h2", null, "I just tried to break the rule"),
    h("p", { class: "meta" }, "These were real attempts made by the assistant's own code, not a simulation. Each one was stopped by the lock before it happened."),
    h("table", { class: "tests" }, r.results.map(x => h("tr", null,
      h("td", null, x.attempt, h("span", { class: "note" }, x.note)),
      h("td", null, h("span", { class: "badge " + (x.blocked ? "green" : "red") }, x.blocked ? "BLOCKED" : "NOT BLOCKED"))))),
    h("p", { class: "big" }, all ? "All " + r.results.length + " attempts failed. The lock held." : "Warning: the lock did not hold."),
    checkYourself(),
  ]);
};
function checkYourself() {
  return h("div", { class: "check" }, h("b", null, "Don't take my word for it. "),
    "Turn Wi-Fi off: everything still works. Or, in a terminal, run ", h("code", null, "python3 tools/fingerprint.py <notes folder>"),
    " before and after a session: it prints the same fingerprint if not one byte changed. The lock is about 150 lines in ",
    h("code", null, "secondbrain/guard.py"), " and reads top to bottom.");
}
$("end").onclick = async () => {
  const r = (await api("/api/end", {}));
  const x = r.receipt;
  lastFolders = lastChips = ""; EVIDENCE = [];
  clear($("prep-out")); clear($("ask-out"));
  apply(r.state);
  const untouched = !x.notes_changed.length && !x.notes_missing.length;
  openModal([
    h("h2", null, "Session ended. Here is your receipt."),
    h("div", { class: "counts" },
      h("div", { class: "count" }, h("span", null, "Notes read"), h("b", null, x.files_read)),
      h("div", { class: "count zero" }, h("span", null, "Sent off this device"), h("b", null, x.sent_off_device)),
      h("div", { class: "count zero" }, h("span", null, "Copies saved"), h("b", null, x.written_to_disk))),
    h("p", { class: "big" }, untouched
      ? "All " + plural(x.notes_checked, "note") + " I read are byte-for-byte identical to when I first opened them."
      : "Some notes changed during the session (not by me: I cannot write)."),
    untouched ? null : h("p", null, "Changed: " + x.notes_changed.concat(x.notes_missing).join(", ")),
    h("ul", null,
      h("li", null, "Forgotten: " + plural(x.forgotten.notes, "note") + ", " + plural(x.forgotten.search_entries, "search entry", "search entries") +
        (x.forgotten.meaning_vectors ? ", " + x.forgotten.meaning_vectors + " meaning vectors" : "") + ", and your folder choices."),
      h("li", null, plural(x.model_calls, "request") + " went to the model on this device" +
        (x.models_unloaded.length ? "; I asked it to unload (" + x.models_unloaded.join(", ") + ")." : ".")),
      h("li", null, plural(x.blocked, "attempt") + " to break the rule blocked."),
      h("li", null, "Files I opened: " + (x.files.length ? x.files.join(", ") : "none") + ".")),
    h("p", { class: "meta" }, "This receipt is on screen only. Close it and it is gone too."),
    checkYourself(),
  ]);
};

// Tell the app when its window closes, so it can forget everything and quit.
// (A reload says this too; the app waits a few seconds to see if the page comes back.)
window.addEventListener("pagehide", () => { try { navigator.sendBeacon("/api/bye", ""); } catch (e) { /* closing anyway */ } });

poll();
