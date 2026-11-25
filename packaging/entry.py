"""Entry point of the one-file build (PyInstaller cannot start from a console-script entry)."""

import multiprocessing
import sys

from kalan.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
