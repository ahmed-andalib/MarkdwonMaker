"""
main.py — Application entry point.

Order of operations matters here: bootstrap.py is imported first and is
the ONLY thing allowed to run before we know the required third-party
packages are installed. Only after ensure_dependencies() succeeds do we
import ui.py, which in turn imports markitdown/pymupdf4llm/pdfplumber/
tkinterdnd2 — modules that would otherwise crash the app at import time
on a fresh machine.
"""

from __future__ import annotations

import sys


def main() -> int:
    import bootstrap

    if not bootstrap.ensure_dependencies():
        print(
            "Markdown Maker cannot start because required dependencies are "
            "missing. See the message above for manual installation "
            "instructions.",
            file=sys.stderr,
        )
        return 1

    # Optional OCR setup — only prompts the very first time the app runs
    # (or when the user later clicks "Configure OCR…" in the app itself).
    # Declining or the install failing is not fatal: the app runs fine
    # without OCR, just with reduced accuracy on scanned/image files.
    bootstrap.ensure_ocr_setup()

    # Optional advanced PDF parsing (Docling) — same opt-in pattern as OCR.
    # Declining or the install failing is not fatal: the app falls back to
    # the fast built-in cascade for every PDF, same as before this existed.
    bootstrap.ensure_docling_setup()

    import ui

    ui.launch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
