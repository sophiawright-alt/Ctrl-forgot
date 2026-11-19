"""Passcodes for protected folders.

A folder listed under `protect` in settings.txt stays off until someone types
the passcode. This is a lock on what the assistant may open, not encryption:
the files themselves are ordinary files on disk.

The passcode in settings.txt can be written plainly (`passcode = 4821`) or,
better, as a one-way hash made by `python tools/passcode.py`, so nobody can
read it from the file.
"""
from __future__ import annotations

import hashlib
import hmac
import os

ITERATIONS = 200_000


def make(passcode):
    """A one-way, salted hash of a passcode, for settings.txt."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2:sha256:%d:%s:%s" % (ITERATIONS, salt.hex(), digest.hex())


def check(stored, given):
    """Does `given` match the passcode in settings.txt (hashed or plain)?"""
    if not stored or given is None:
        return False
    given = str(given)
    if stored.startswith("pbkdf2:"):
        try:
            _, algo, iterations, salt, digest = stored.split(":")
            got = hashlib.pbkdf2_hmac(algo, given.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(got.hex(), digest)
    return hmac.compare_digest(stored.encode("utf-8"), given.encode("utf-8"))
