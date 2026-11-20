# Windows launcher: .pyw runs under pythonw (no console). Equivalent to `python run.py --app`.
import os
import sys

sys.dont_write_bytecode = True              # no .pyc files
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
sys.argv = [os.path.join(HERE, "run.py"), "--app"] + sys.argv[1:]

import run                                   # noqa: E402

run.main()
