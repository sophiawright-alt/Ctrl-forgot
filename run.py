#!/usr/bin/env python3
"""Start Second Brain.

    python3 run.py                       # uses the sample notes
    python3 run.py --notes ~/my-notes    # point it at another folder
    python3 run.py --app                 # open in its own window; closing it quits

Or double-click "Second Brain.pyw" (Windows) / "Second Brain.command" (Mac),
which run it with --app and no terminal. Settings for those go in settings.txt.

Otherwise open the address it prints. Needs Python 3.8+ and nothing else. For the
written summaries and for reading handwriting it uses a model running on this
machine through Ollama (see README).
"""
import argparse
import datetime
import os
import sys
import threading

if sys.version_info < (3, 8):
    sys.exit("Second Brain needs Python 3.8 or newer. You have %s." % sys.version.split()[0])

sys.dont_write_bytecode = True               # not even compiled-code caches get written
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from secondbrain import guard, server                      # noqa: E402
from secondbrain.ledger import Ledger                      # noqa: E402
from secondbrain.llm import LocalModel                     # noqa: E402
from secondbrain.session import Session                    # noqa: E402


def main():
    from secondbrain import desktop
    settings = desktop.read_settings(os.path.join(HERE, "settings.txt"))
    ap = argparse.ArgumentParser(description="A local, forgetful assistant for a manager's scattered notes.")
    ap.add_argument("--notes", default=settings.get("notes") or os.path.join(HERE, "sample_notes"),
                    help="folder of notes (default: sample_notes, or 'notes' in settings.txt)")
    ap.add_argument("--port", type=int, default=int(settings.get("port") or 8765))
    ap.add_argument("--ollama-port", type=int, default=int(settings.get("ollama_port") or 11434),
                    help="port of Ollama on THIS machine")
    ap.add_argument("--today", default=settings.get("today"), help="pretend today is YYYY-MM-DD (for rehearsing a demo)")
    ap.add_argument("--app", action="store_true", help="open in its own window, and quit when it is closed")
    args = ap.parse_args()
    for key, env in (("model", "SECOND_BRAIN_MODEL"), ("vision_model", "SECOND_BRAIN_VISION_MODEL"),
                     ("context", "SECOND_BRAIN_CONTEXT")):
        if settings.get(key) and not os.environ.get(env):
            os.environ[env] = settings[key]

    if args.app:
        running = desktop.already_running(args.port)
        if running:                                  # double-clicked again: just show the window
            desktop.open_window(running)
            return

    notes = os.path.expanduser(args.notes)
    if not os.path.isabs(notes) and not os.path.isdir(notes):
        notes = os.path.join(HERE, notes)            # "notes = demo_notes" means the folder next to run.py
    notes = os.path.abspath(notes)
    if not os.path.isdir(notes):
        message = "Notes folder not found:\n%s\n\nCheck the 'notes' line in settings.txt." % notes
        if args.app:
            desktop.tell(message)
        sys.exit(message)
    today = datetime.date.fromisoformat(args.today) if args.today else None

    ledger = Ledger()
    model = LocalModel(ledger, port=args.ollama_port)
    session = Session(notes, ledger, model, today=today,
                      protected=[p for p in (settings.get("protect") or "").split(",")],
                      passcode=settings.get("passcode"))
    httpd, port = server.serve(session, ledger, args.port)
    url = "http://127.0.0.1:%d" % port
    if args.app:
        # The only program this app ever starts: its own window, before the lock goes on.
        how = desktop.open_window(url)
        httpd.lifeline = desktop.Lifeline(lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())

    # From this line on, the process cannot write to disk, reach the network,
    # start other programs, or read notes the user has not switched on.
    guard.arm(notes, exempt_dirs=[os.path.join(HERE, "web"), os.path.join(HERE, "secondbrain")],
              is_allowed=session.is_allowed, on_event=ledger.record)

    status = model.refresh(force=True)
    print("")
    print("  Second Brain is running on this computer only.")
    print("  Open:   %s%s" % (url, "   (opened in: %s)" % how if args.app else ""))
    print("  Notes:  %s" % notes)
    if status["running"] and status["chat"]:
        print("  Model:  %s (local)%s" % (status["chat"],
              "" if status["vision"] else "   [no local model that can read images: handwriting is off]"))
    else:
        print("  Model:  none found. Search and meeting prep still work; summaries and handwriting need Ollama (see README).")
    print("  Lock:   on. No writing to disk, no network beyond this machine.")
    print("  Stop:   %s" % ("close the app window" if args.app else "Ctrl+C (everything in memory is then gone)"))
    print("")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        r = session.end()
        print("\n  Session ended. Forgot %d notes and %d search entries. Wrote 0 files. Sent 0 bytes off this device."
              % (r["forgotten"]["notes"], r["forgotten"]["search_entries"]))


if __name__ == "__main__":
    main()
