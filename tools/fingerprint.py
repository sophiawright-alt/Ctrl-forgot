#!/usr/bin/env python3
"""An independent check that has nothing to do with the assistant.

    python3 tools/fingerprint.py path/to/notes

Prints one fingerprint for the whole folder (every file's path, size and
contents). Run it before a session and again after. If the two lines match,
not one byte in the folder was added, changed or removed.
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
