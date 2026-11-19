# Demo guide (the `demo_notes` set)

A fictional AI team in a pharmaceutical company is rolling out **Scribe**, an AI assistant that drafts clinical study report summaries for medical writers. You are the team's manager. Every person, project, vendor and figure is made up.

| Person | Role |
|---|---|
| Aisha | ML engineer, wants to lead the Scribe pilot |
| Marcus | Data engineer, swamped by a data migration |
| Leila | Validation (QA) lead |
| Ben | Product owner for the medical writers |
| Raj | Finance partner |
| Grace | Information security |
| Elena | Your director |
| Tomas | Engineer at Halden (the chosen vendor) |

Halden was chosen; **Quillon** was ruled out because data would leave your environment.

## Set up (once)

In **settings.txt**:
```
notes = demo_notes
today = 2026-11-23
protect = private
```
plus your `passcode =` line. `today` keeps "2 weeks ago" and "overdue" the same whenever you record.

## Your two handwritten pages

The two `.jpg` pages in `demo_notes/notes` are computer-drawn stand-ins. Write these out by hand in dark pen, photograph them, save as JPG (about 1500 pixels wide), and save over the stand-ins **with the same file names**.

**`2026-11-17 notebook page.jpg`**
```
Tues 17 Nov - corridor chat w/ Aisha
A wants to LEAD the Scribe pilot
said yes in principle !!
but only if Marcus takes back
the pipeline work -> ask Ben
conference budget sorted - Raj ok'd
£750 from training. tell her YES
do before 1:1 on Mon 23rd
```

**`2026-11-18 whiteboard photo.jpg`** (a whiteboard or a page both work)
```
Wed 18 Nov - whiteboard, pilot risks
1. hallucination rate now 1.4%
   (target under 2%) - OK
2. InfoSec sign-off: still waiting on Grace !!
3. who monitors Scribe after go-live??
   nobody yet
decided: weekly review of 20 drafts
```

## Before you press record

1. Start the app. Switch on **notes** only. Wait until both photos and the chat screenshot show their labels (about a minute), then do one practice prep so the model is loaded.
2. Click **End session and forget everything**, so the recording starts blind with 0 notes read.

## Three-minute script

| Time | Do | Say (criterion) |
|---|---|---|
| 0:00 | Show the app: nothing switched on, receipt at 0. | "Monday, 1:1 with Aisha in ten minutes. My notes are everywhere: meetings, a Word doc, a PDF, a chat screenshot, my notebook." (Insight) |
| 0:15 | Switch on **notes**. Point at the receipt filling up. | "I choose what it can see. Every file it opens appears here. Nothing leaves this laptop." (Knowledge stays yours) |
| 0:30 | **Prepare for a meeting → Aisha.** | "One brief, from fourteen scattered notes." (Working end to end) |
| 0:45 | Point at **Probably done**: *find out about conference budget* ← *conference budget sorted* (from the handwritten page). Click the citation to show the photo beside the transcription. | "It connected a to-do from October with my handwriting from last week." (Creative AI; more than one kind of content) |
| 1:05 | Point at **Still open**: *haven't answered her on the conference*, and **Decisions**: *said yes in principle*. | "And it tells me what I still owe her." |
| 1:15 | Point at the **3.1% → 1.4%** change. | "Older notes disagree with newer ones. It shows both and trusts the newer." (Copes with messy, out-of-date notes) |
| 1:25 | **Find → "When does the Scribe pilot start?"** Show **2 Nov → 30 Nov** and that the unsent director update still says 2 Nov. | "My draft to my director is wrong, and it caught that." |
| 1:45 | **Find → "When does the Quillon pilot start?"** | "There is no Quillon pilot: we ruled it out. It says so rather than guessing, and shows why." (Trust: honest when unsure) |
| 2:00 | **Find → "What is Aisha's salary?"** Then click **private**: type a wrong passcode, then the right one, then switch it off again. | "Private notes stay locked until I say. The wrong attempt is on the receipt. Switch off, and it forgets." (User controls access) |
| 2:20 | Turn **Wi-Fi off**. Ask any question again. | "No internet needed: the AI runs on this laptop." |
| 2:30 | Click **Try to break the rule**. | "It genuinely tries to send data out and save a copy. Every attempt is blocked." |
| 2:45 | Click **End session and forget everything**. | "0 sent, 0 saved, every note byte-for-byte unchanged, and it's all gone from memory." |

Keep your honest limits for a closing line or the email: the lock is inside the app (not an operating-system sandbox), and handwriting reading can make mistakes, which is why the photo is always shown next to the transcription.
