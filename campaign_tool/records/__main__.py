"""Compatibility module entry point for the records command line."""
from .cli import COMMANDS, main

if __name__ == "__main__":
    raise SystemExit(main())
