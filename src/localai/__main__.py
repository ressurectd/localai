"""Enables `python -m localai`."""

import sys

from localai.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
