"""PyInstaller entry point. `python -m agent` is the equivalent in a checkout."""

import sys

from agent.__main__ import cli

if __name__ == "__main__":
    sys.exit(cli())
