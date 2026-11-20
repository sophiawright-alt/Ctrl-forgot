"""Passcodes for protected folders.

Stored in settings.txt either in plain text or as a PBKDF2-SHA256 hash
(see tools/passcode.py). This restricts what the assistant may open; it does
not encrypt files on disk.
"""
from __future__ import annotations

import hashlib
import hmac
import os

ITERATIONS = 200_000


def make(passcode):
    """Salted PBKDF2-SHA256 hash string for settings.txt."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2:sha256:%d:%s:%s" % (ITERATIONS, salt.hex(), digest.hex())


def check(stored, given):
    """Constant-time comparison against a plain or hashed stored passcode."""
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
