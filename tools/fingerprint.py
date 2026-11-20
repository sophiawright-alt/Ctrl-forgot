#!/usr/bin/env python3
"""Print a single SHA-256 fingerprint for a folder (paths and contents).

    python tools/fingerprint.py path/to/notes

Independent of the app: run before and after a session to confirm nothing
was added, changed or removed.
"""
import hashlib
import os
import sys

root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "sample_notes")
total, count = hashlib.sha256(), 0
for folder, dirs, files in os.walk(root):
    dirs.sort()
    for name in sorted(files):
        path = os.path.join(folder, name)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        total.update(("%s\0%s\n" % (os.path.relpath(path, root), digest)).encode("utf-8"))
        count += 1
print("%s  (%d files in %s)" % (total.hexdigest()[:32], count, root))
