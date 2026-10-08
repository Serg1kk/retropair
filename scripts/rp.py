#!/usr/bin/env python3
"""Canonical RetroPair CLI; sr.py remains a supported compatibility entrypoint."""
from sr import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
