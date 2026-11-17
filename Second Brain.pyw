# Double-click this file to start Second Brain in its own window, with no terminal.
# (Windows runs .pyw files with "pythonw", which has no console window.)
# Closing the window ends the session and forgets everything.
import os
import sys

sys.dont_write_bytecode = True              # write nothing, not even compiled-code caches
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
sys.argv = [os.path.join(HERE, "run.py"), "--app"] + sys.argv[1:]

import run                                   # noqa: E402

run.main()
