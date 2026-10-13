"""Running a free-threaded build without its GIL.

An extension not declared free-threading safe (lxml before 7.0) turns a
free-threaded interpreter's GIL back on at import unless PYTHON_GIL=0 was
set before the interpreter started; nothing at run time turns it off
again. An entry point that finds it on after its imports therefore reruns
itself with it set (`relaunch_without_gil`); with every extension declared
safe, nothing reruns.
"""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import sysconfig


def relaunch_argv(argv: list[str], free_threaded: bool, gil_flag: int | None,
                  gil_on: bool) -> list[str] | None:
    """`argv` when this process must rerun with PYTHON_GIL=0: a free-threaded
    build (`free_threaded`) started without PYTHON_GIL (`gil_flag`, i.e.
    sys.flags.gil, None) whose GIL an import turned on (`gil_on`).
    Otherwise None.

    >>> relaunch_argv(["py", "harvest.py"], free_threaded=True, gil_flag=None, gil_on=True)
    ['py', 'harvest.py']
    >>> relaunch_argv(["py", "harvest.py"], free_threaded=True, gil_flag=None, gil_on=False) is None
    True
    >>> relaunch_argv(["py", "harvest.py"], free_threaded=True, gil_flag=1, gil_on=True) is None
    True
    >>> relaunch_argv(["py", "harvest.py"], free_threaded=False, gil_flag=None, gil_on=True) is None
    True
    """
    return argv if free_threaded and gil_flag is None and gil_on else None


def relaunch_without_gil(*modules: str) -> None:
    """Import `modules` (what the run loads, beyond what the caller already
    has), then, if that left a free-threaded build's GIL on and PYTHON_GIL
    was unset, rerun this command with PYTHON_GIL=0 and exit with its
    status; otherwise return. A Nuitka binary reruns itself, a script its
    interpreter and flags."""
    for name in modules:
        importlib.import_module(name)
    argv = relaunch_argv(sys.argv if "__compiled__" in globals()
                         else [sys.executable, *sys.orig_argv[1:]],
                         free_threaded=bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
                         gil_flag=getattr(sys.flags, "gil", None),
                         gil_on=getattr(sys, "_is_gil_enabled", lambda: True)())
    if argv is None:
        return
    os.putenv("PYTHON_GIL", "0")   # for the child and what it starts in turn
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl+C is the child's to handle
    sys.exit(subprocess.call(argv))
