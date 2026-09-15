"""The web shell's static payload: exactly one file.

`index.html` is the whole front end -- no CDN, no framework, no build step, no Qt. It is data,
not code, so it lives in a package of its own and is found through `page()` rather than through
a path guessed by the caller.
"""
from __future__ import annotations

import pathlib

__all__ = ["DIR", "page", "page_text"]

DIR = pathlib.Path(__file__).resolve().parent
"""Directory that holds index.html (and nothing else that is served)."""


def page() -> pathlib.Path:
    """Path of the single page the server serves at `/`."""
    return DIR / "index.html"


def page_text() -> str:
    return page().read_text(encoding="utf-8")
