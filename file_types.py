"""
file_types.py — Detects and labels a file's type for display in the queue
list, and flags whether it will need OCR to extract meaningful content.

Detection is intentionally cheap: for PDFs we only look at a handful of
sample pages (not the whole document) so this stays fast enough to run on
every file the moment it's added to the queue, even for large batches.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Extensions handled directly by MarkItDown with no ambiguity about type.
_DOCUMENT_LABELS = {
    ".docx": "Word Document", ".doc": "Word Document",
    ".pptx": "PowerPoint", ".ppt": "PowerPoint",
    ".xlsx": "Excel Spreadsheet", ".xls": "Excel Spreadsheet",
    ".csv": "CSV", ".tsv": "TSV",
    ".html": "HTML", ".htm": "HTML",
    ".txt": "Plain Text",
    ".json": "JSON", ".xml": "XML",
    ".epub": "EPUB",
}

# Public so ui.py can include these in its accepted-file-extensions set.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".gif", ".webp"}

# Sampling limits for PDF scanned-vs-text detection.
_PDF_SAMPLE_PAGE_LIMIT = 5
_MIN_CHARS_PER_PAGE_FOR_TEXT = 25


@dataclass
class DetectedType:
    label: str  # short, human-readable — shown directly in the UI's Type column
    category: str  # "text_pdf" | "scanned_pdf" | "mixed_pdf" | "image" | "document" | "unknown"
    needs_ocr: bool
    detail: str = ""  # optional longer explanation, e.g. page counts


def detect_type(path: Path) -> DetectedType:
    extension = path.suffix.lower()

    if extension in IMAGE_EXTENSIONS:
        return DetectedType(
            label="Image File", category="image", needs_ocr=True,
            detail="Requires OCR to extract any text.",
        )

    if extension in _DOCUMENT_LABELS:
        return DetectedType(
            label=_DOCUMENT_LABELS[extension], category="document", needs_ocr=False,
        )

    if extension == ".pdf":
        return _detect_pdf_type(path)

    return DetectedType(label=f"{extension.upper().lstrip('.')} File" if extension else "Unknown",
                         category="unknown", needs_ocr=False)


def _detect_pdf_type(path: Path) -> DetectedType:
    try:
        import pymupdf as fitz  # PyMuPDF's modern import name (fitz is deprecated)
    except ImportError:
        return DetectedType(label="PDF", category="text_pdf", needs_ocr=False,
                             detail="Type detection unavailable (PyMuPDF not installed).")

    try:
        with fitz.open(str(path)) as doc:
            page_count = doc.page_count
            if page_count == 0:
                return DetectedType(label="PDF (empty)", category="unknown", needs_ocr=False)

            sample_count = min(page_count, _PDF_SAMPLE_PAGE_LIMIT)
            text_pages = 0
            scanned_pages = 0

            for page_index in range(sample_count):
                page = doc.load_page(page_index)
                text = page.get_text("text") or ""
                has_meaningful_text = len(text.strip()) >= _MIN_CHARS_PER_PAGE_FOR_TEXT
                has_images = len(page.get_images(full=True)) > 0

                if has_meaningful_text:
                    text_pages += 1
                elif has_images:
                    scanned_pages += 1
                # A page with neither text nor images (blank page) counts
                # toward neither bucket.

        if text_pages == sample_count:
            return DetectedType(
                label="PDF — Text", category="text_pdf", needs_ocr=False,
                detail=f"{page_count} page(s), text extracted normally.",
            )
        if scanned_pages == sample_count or (scanned_pages > 0 and text_pages == 0):
            return DetectedType(
                label="PDF — Scanned (OCR)", category="scanned_pdf", needs_ocr=True,
                detail=f"{page_count} page(s) appear to be scanned images with no text layer.",
            )
        if text_pages > 0 and scanned_pages > 0:
            return DetectedType(
                label="PDF — Mixed (OCR)", category="mixed_pdf", needs_ocr=True,
                detail=f"{page_count} page(s): some text, some scanned — OCR will fill the gaps.",
            )

        # Sampled pages had neither clear text nor images (e.g. all blank,
        # or vector-only content) — default to treating it as a text PDF
        # and let the normal extraction cascade handle it.
        return DetectedType(label="PDF", category="text_pdf", needs_ocr=False,
                             detail=f"{page_count} page(s).")

    except Exception as exc:  # noqa: BLE001 - detection must never crash file ingestion
        return DetectedType(label="PDF (type unknown)", category="unknown", needs_ocr=False,
                             detail=f"Could not inspect file: {exc}")
