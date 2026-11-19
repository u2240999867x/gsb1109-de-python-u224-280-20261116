# -*- coding: utf-8 -*-
"""Entry point so the tool runs as ``python -m tools.attribution``."""

from .contact_attribution import main

if __name__ == "__main__":
    raise SystemExit(main())
