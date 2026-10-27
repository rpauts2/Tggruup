"""Entry point for the packaged desktop build.

PyInstaller needs a single script to freeze. It exists so the packaged app and
`python -m tgfarm.cli desktop` run the same code path — a second launcher would
be a second thing to keep working.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Required before anything spawns a process in a frozen build, otherwise
    # each worker re-runs this module and the app starts N times.
    multiprocessing.freeze_support()

    from tgfarm.desktop import main as desktop_main

    argv = [arg for arg in sys.argv[1:] if not arg.startswith("--multiprocessing")]
    return desktop_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())