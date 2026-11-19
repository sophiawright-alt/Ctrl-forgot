#!/usr/bin/env python3
"""Make a passcode line for settings.txt that does not show the passcode itself.

    python tools/passcode.py

Type the passcode twice (nothing shows as you type). It prints one line; paste
it into settings.txt in place of any existing "passcode =" line. Nothing is
saved anywhere.
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
