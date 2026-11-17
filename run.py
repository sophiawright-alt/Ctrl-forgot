#!/usr/bin/env python3
"""Start Second Brain.

    python3 run.py                       # uses the sample notes
    python3 run.py --notes ~/my-notes    # point it at another folder

Then open the address it prints. Needs Python 3.8+ and nothing else. For the
written summaries and for reading handwriting it uses a model running on this
machine through Ollama (see README).
"""
import argparse
import datetime
import os
import sys

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
    ap = argparse.ArgumentParser(description="A local, forgetful assistant for a manager's scattered notes.")
    ap.add_argument("--notes", default=os.path.join(HERE, "sample_notes"), help="folder of notes (default: sample_notes)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ollama-port", type=int, default=11434, help="port of Ollama on THIS machine")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (useful for rehearsing a demo)")
    args = ap.parse_args()

    notes = os.path.abspath(os.path.expanduser(args.notes))
    if not os.path.isdir(notes):
        sys.exit("Notes folder not found: %s" % notes)
    today = datetime.date.fromisoformat(args.today) if args.today else None

    ledger = Ledger()
    model = LocalModel(ledger, port=args.ollama_port)
    session = Session(notes, ledger, model, today=today)

    # From this line on, the process cannot write to disk, reach the network,
    # start other programs, or read notes the user has not switched on.
    guard.arm(notes, exempt_dirs=[os.path.join(HERE, "web"), os.path.join(HERE, "secondbrain")],
              is_allowed=session.is_allowed, on_event=ledger.record)

    httpd, port = server.serve(session, ledger, args.port)
    status = model.refresh(force=True)
    print("")
    print("  Second Brain is running on this computer only.")
    print("  Open:   http://127.0.0.1:%d" % port)
    print("  Notes:  %s" % notes)
    if status["running"] and status["chat"]:
        print("  Model:  %s (local)%s" % (status["chat"],
              "" if status["vision"] else "   [no local model that can read images: handwriting is off]"))
    else:
        print("  Model:  none found. Search and meeting prep still work; summaries and handwriting need Ollama (see README).")
    print("  Lock:   on. No writing to disk, no network beyond this machine.")
    print("  Stop:   Ctrl+C (everything in memory is then gone)")
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
