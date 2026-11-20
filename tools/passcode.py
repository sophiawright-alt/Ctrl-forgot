#!/usr/bin/env python3
"""Generate a hashed `passcode =` line for settings.txt.

    python tools/passcode.py

Prompts for the passcode twice and prints the line. Nothing is written to disk.
"""
import getpass
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from secondbrain.passcode import make  # noqa: E402

first = getpass.getpass("Passcode: ")
if len(first) < 4:
    sys.exit("Please use at least 4 characters.")
if getpass.getpass("Same again: ") != first:
    sys.exit("Those did not match. Nothing was changed.")
print("\nPaste this line into settings.txt:\n")
print("passcode = " + make(first))
print("")
