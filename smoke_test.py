#!/usr/bin/env python3
"""Deprecated shim — the test suite moved to pytest under tests/.

    pytest                                  # everything
    pytest tests/test_store.py -k closed     # one area

Kept so muscle memory and older docs keep working.
"""
import subprocess
import sys

if __name__ == "__main__":
    print("smoke_test.py is deprecated - running `pytest` instead.\n")
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]]))
