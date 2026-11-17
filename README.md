# Second Brain

A personal assistant for a manager's scattered notes that **uses what you already wrote down and keeps none of it**.

Built for the AI Studio and Newpage hackathon brief, "Second Brain for Managers".

The moment it fixes: **you have a meeting in ten minutes and what you know about it is spread across meeting notes, a running 1:1 doc, a to-do list, a half-written update, a page in your notebook and a screenshot of a chat.** Type a name or a topic and get one brief: what is still open, what is probably already done, what was decided, what has changed since you wrote it down, and what is unresolved. Every line links to the exact line of the exact note it came from.

## Run it (about 2 minutes)

You need Python 3.8 or newer. There is nothing to install for the app itself.

```
cd second-brain
python3 run.py          # Windows: python run.py
```

Open the address it prints (http://127.0.0.1:8765). It starts on the fictional sample notes in `sample_notes/`. To use another folder: `python3 run.py --notes path/to/folder`.

At this point search, meeting prep, change detection and the receipt all work with no AI model at all.

## Add the local model (for written summaries and handwriting)

1. Install Ollama from https://ollama.com and start it.
2. Download a model once (this is the only step that needs the internet):

   ```
   ollama pull qwen3-vl:8b          # about 6 GB. Reads handwriting well. Recommended.
   ollama pull gemma3:4b            # about 3 GB. Lighter alternative if your laptop struggles.
   ollama pull nomic-embed-text     # optional, small. Adds search by meaning, not just by words.
   ```

3. Restart `python3 run.py`. The pill at the top right should say "Model on this device: …".

The app picks a model automatically. To force one: `SECOND_BRAIN_MODEL=gemma3:4b python3 run.py`. Any Ollama model marked as a cloud model is ignored.

## What it can read

| Kind | Files | How |
|---|---|---|
| Typed notes | `.txt`, `.md`, `.csv` | Read directly |
| Word | `.docx` | Read from memory; nothing is unpacked to disk |
| PDF | `.pdf` | Text is read directly. Scanned PDFs (pictures of pages) go to the local vision model, like a photo |
| Handwriting, whiteboards, screenshots | `.jpg`, `.png` | Local vision model |

Not read: old `.doc` files (save as `.docx`), locked PDFs (the app says so; use Print, then "Microsoft Print to PDF", to make an unlocked copy), and pictures pasted inside Word files.

## Handwriting

Put a photo of a handwritten page (JPG or PNG; export iPhone HEIC photos as JPG first) in a folder under your notes folder and switch that folder on. The local vision model transcribes it, in memory only. The transcription is then searchable and citable like any other note, marked "read from image", and clicking a citation shows the photo next to the transcription so you can check it. Words the model was unsure of are marked `[?]`.

`sample_notes/photos/2026-11-10 notebook page.jpg` is computer-drawn so the sample set works out of the box. **For your demo, replace it with a real photo**: write these lines by hand, photograph them, and save over that file (keep the date at the start of the file name):

```
Tues 10 Nov - corridor chat w/ Priya
P wants to LEAD the Helix pilot
said yes in principle !!
but only if backfill sorted -> ask Dan, 2 days/wk from Kofi?
conference: tell her YES (budget ok - Mei confirmed £600)
do before 1:1 on the 16th
```

## A 3-minute demo script

Rehearse with `python3 run.py --today 2026-11-16` so "3 weeks ago" labels stay the same whatever day you record.

1. **Start blind.** Receipt shows 0 notes read. "I can't see anything yet." (Knowledge stays yours: you choose what it sees.)
2. Switch on `1-1s`, `meetings`, `scratch`, `photos`. **Leave `private-hr` off.** Watch the receipt list each file as it is read, and the photos being read by the local model.
3. Click **Priya**. Walk down the brief:
   - "Probably done or overtaken": the forecast action from 6 Oct was never ticked off, but the 1:1 note of 14 Oct says "forecast sent". Two notes that never met. (Connect.)
   - "Still open": you promised to find out about conference budget four weeks ago.
   - "Decisions on record": "said yes in principle" comes from the handwritten page. Click the number to see the photo beside the transcription. (More than one kind of content.)
   - The amber box: "I have not looked in the folder you switched off: private-hr". (Honest about what it has not seen.)
   - "In short": the model's summary. Every point has source numbers; it says how many points it threw away because it could not trace them to a note.
4. Tab **Find something I wrote down** → "What did we decide about the Helix budget?" → **£40k → £28k**: your notes disagree, the newer one wins, and the unsent director update still has the old number. (Messy and out of date is normal.)
5. Ask "What is Priya's salary?" → "That is not in the notes I can see." (Honest when unsure.) Then switch `private-hr` on to show it is your choice, and off again: "Forgot 2 notes."
6. Click **Try to break the rule**. The assistant really tries to reach the internet, save a copy and start another program. All blocked.
7. **Turn Wi-Fi off** and run another prep. It still works.
8. Click **End session and forget everything**. Final receipt: notes byte-for-byte identical, 0 sent, 0 saved. In a terminal, `python3 tools/fingerprint.py sample_notes` prints the same fingerprint before and after.

## How the one rule is upheld

| Claim | How | Where |
|---|---|---|
| Nothing leaves the device | The process is blocked from connecting to anything but 127.0.0.1. The model runs locally through Ollama. The web page is served with a policy that stops it loading anything from elsewhere. | `secondbrain/guard.py`, `llm.py`, `server.py` |
| No copy is kept | The process is blocked from opening any file for writing, and from creating, moving or deleting files. The search index, transcriptions and receipt live in memory only and are rebuilt each session. There is no database and no cache. | `guard.py`, `index.py`, `session.py` |
| You choose what it sees | Every session starts with every folder off. The lock itself refuses to open or list anything in a folder that is off, so this is not just a filter in the interface. Switching off drops those notes from memory at once. | `guard.py`, `session.py` |
| You can check | Live receipt generated by the lock; "Try to break the rule"; a before/after fingerprint of every note at session end; `tools/fingerprint.py` as a check that does not involve the app; Wi-Fi off. The code has no third-party packages, so there is nothing hidden to audit. | `ledger.py`, `tools/` |
| Honest when unsure | "Not in your notes" instead of a guess; the model is not even asked when search finds nothing; every model sentence is checked against the notes it cites and dropped if it does not hold up; hidden folders, unread images and old notes are stated. | `brief.py` |

### What this does not prove (say this in the pitch, judges will ask)

- The lock is inside this one Python process. It is a strong seatbelt for a prototype, not an operating-system sandbox. A production build would add one (for example a sandboxed desktop app with no network entitlement).
- Ollama is a separate program. This app only ever talks to it on this machine and never uses cloud models, but the app cannot police what Ollama itself does. Turning Wi-Fi off is the simple proof; downloading models needs the internet once.
- While a session is open, your notes are in this computer's memory (and the model's memory). They are released when you end the session or stop the app.
- Your browser shows what the app sends it. The app tells the browser not to cache anything, and uses no browser storage.
- Handwriting recognition makes mistakes. That is why transcriptions are always shown beside the original photo and never treated as certain.

## What is in here

```
run.py                   start here
secondbrain/guard.py     the lock (no writes, no network, no other programs, only allowed folders)
secondbrain/ledger.py    the receipt
secondbrain/session.py   folder switches, loading notes, reading images, forgetting
secondbrain/index.py     in-memory search (keywords, typos, synonyms)
secondbrain/pdftext.py   reading PDFs with nothing but Python
secondbrain/dates.py     "3 Nov", "by Fri", "EOW"
secondbrain/extract.py   actions, decisions, questions, what changed, what looks done
secondbrain/brief.py     meeting prep and find; the check on the model's answers
secondbrain/llm.py       the local model (Ollama) and its prompts
secondbrain/server.py    local web server
web/                     the page (plain HTML, CSS, JavaScript). Looks: web/style.css
sample_notes/            fictional manager notes, made up for this prototype
tools/fingerprint.py     independent "did anything change?" check
tools/make_sample_images.py   regenerates the sample images (development only, needs Pillow)
tools/make_sample_pdf.py      regenerates the sample PDF (development only, needs reportlab)
tests/                   python3 -B tests/test_all.py
```

## Ideas for building on it

- More content: calendar exports, email exports, voice memos (local speech-to-text).
- A "capture" moment: paste rough notes, get actions and decisions pulled out, still without saving.
- Stream the model's answer word by word so it feels faster on slow laptops.
- Per-meeting scopes ("for this 1:1, only these two folders").
- An operating-system sandbox around the whole app.
- Package as a desktop app so there is no terminal step.

All sample notes are fictional. Do not point this at anyone's real notes for the hackathon demo.
