"""
converter.py — Multi-engine document -> Markdown conversion orchestrator.

Dispatch strategy
------------------
- Non-PDF files (.docx, .xlsx, .pptx, .csv, .html, .txt, images, etc.):
  handled directly by MarkItDown, which already does a solid job for these
  formats.

- PDF files: run through a three-stage fallback cascade, since a single
  extraction strategy is never reliable across the full range of academic
  PDF layouts:

    1. pymupdf4llm.to_markdown()  — best general-purpose choice for
       multi-column reading order, headings, and inline GFM tables.
    2. Manual pdfplumber reconstruction — per-page layout-aware text
       extraction plus explicit table detection converted to GFM tables.
       Used if stage 1 raises or returns suspiciously empty content.
    3. MarkItDown() — last-resort plain extraction, so the file is never
       fully skipped if the specialized engines fail outright.

Every stage is wrapped in its own try/except. Failures are recorded on the
ConversionResult rather than raised, so callers (the thread pool workers in
ui.py) never need exception handling of their own around a single file.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import app_config
import file_types

logger = logging.getLogger("markdown_maker.converter")

# Rendering DPI used when rasterizing PDF pages for OCR. Higher is more
# accurate but slower; 200 is a good balance for typical scanned documents.
_OCR_RENDER_DPI = 200

# File extensions MarkItDown handles well directly, without a PDF-specific
# fallback chain.
_MARKITDOWN_DIRECT_EXTENSIONS = {
    ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv", ".tsv",
    ".html", ".htm", ".txt", ".json", ".xml", ".zip", ".epub",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".wav", ".mp3",
}

_PDF_EXTENSION = ".pdf"

# Below this character count, a PDF extraction result is treated as
# suspiciously empty and the next fallback stage is attempted instead.
_MIN_PLAUSIBLE_MARKDOWN_LENGTH = 40


@dataclass
class ConversionResult:
    source_path: Path
    output_path: Optional[Path] = None
    success: bool = False
    engine_used: Optional[str] = None
    markdown: Optional[str] = None
    error_message: Optional[str] = None
    error_traceback: Optional[str] = None
    engine_attempts: List[str] = field(default_factory=list)
    detected_type_label: Optional[str] = None
    warning: Optional[str] = None


def _log_attempt_failure(engine_name: str, exc: Exception) -> str:
    tb = traceback.format_exc()
    logger.warning("Engine '%s' failed for a file: %s", engine_name, exc)
    return tb


def _table_rows_to_gfm(rows: List[List[Optional[str]]]) -> str:
    """Convert a pdfplumber-style table (list of row lists) into a GFM table."""
    cleaned_rows = []
    for row in rows:
        cleaned_rows.append([(cell or "").replace("\n", " ").replace("|", "\\|").strip() for cell in row])

    if not cleaned_rows:
        return ""

    col_count = max(len(r) for r in cleaned_rows)
    for r in cleaned_rows:
        while len(r) < col_count:
            r.append("")

    header, *body = cleaned_rows
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _convert_with_markitdown(path: Path) -> str:
    from markitdown import MarkItDown  # imported lazily; guaranteed present post-bootstrap

    md_engine = MarkItDown()
    result = md_engine.convert(str(path))
    text = getattr(result, "text_content", None) or getattr(result, "markdown", None)
    if not text:
        raise ValueError("MarkItDown returned empty content")
    return text


def _convert_pdf_with_pymupdf4llm(path: Path) -> str:
    import pymupdf4llm  # lazy import

    markdown_text = pymupdf4llm.to_markdown(str(path))
    if not markdown_text or len(markdown_text.strip()) < _MIN_PLAUSIBLE_MARKDOWN_LENGTH:
        raise ValueError("pymupdf4llm returned suspiciously little content")
    return markdown_text


def _convert_pdf_with_pdfplumber(path: Path) -> str:
    import pdfplumber  # lazy import

    page_sections: List[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            page_markdown_parts: List[str] = []

            # Layout-aware text extraction keeps multi-column reading order
            # far more intact than plain page.extract_text().
            try:
                layout_text = page.extract_text(layout=True) or ""
            except Exception:
                layout_text = page.extract_text() or ""
            if layout_text.strip():
                page_markdown_parts.append(layout_text.strip())

            try:
                tables = page.extract_tables()
            except Exception:
                tables = []

            for table_index, table in enumerate(tables, start=1):
                gfm_table = _table_rows_to_gfm(table)
                if gfm_table:
                    page_markdown_parts.append(
                        f"\n\n**Table {table_index} (page {page_index})**\n\n{gfm_table}"
                    )

            # Only emit the page marker (and this page's section at all) if
            # something was actually extracted — otherwise a page full of
            # only image content would silently "succeed" with an empty
            # comment, masking the fact that nothing useful came out of it.
            if page_markdown_parts:
                page_sections.append(f"\n\n<!-- Page {page_index} -->\n" + "\n".join(page_markdown_parts))

    full_markdown = "\n".join(page_sections).strip()
    if not full_markdown:
        raise ValueError("pdfplumber extracted no usable content")
    return full_markdown


def _ocr_settings() -> tuple:
    """Reads the persisted OCR choice. Returns (enabled: bool, engine: Optional[str])."""
    config = app_config.load_config()
    return bool(config.get("ocr_enabled")), config.get("ocr_engine")


# EasyOCR's Reader() loads neural network weights and is expensive to
# construct — cache a single instance per process and guard it with a lock
# since multiple worker threads may need OCR concurrently.
_easyocr_reader = None
_easyocr_lock = None


def _get_easyocr_reader():
    global _easyocr_reader, _easyocr_lock
    import threading

    if _easyocr_lock is None:
        _easyocr_lock = threading.Lock()

    with _easyocr_lock:
        if _easyocr_reader is None:
            import easyocr  # lazy import; only needed if easyocr is the configured engine

            _easyocr_reader = easyocr.Reader(["en"], gpu=False)
        return _easyocr_reader


def _ocr_pil_image(image, engine: str) -> str:
    """Runs OCR on a single PIL Image using the requested engine."""
    if engine == "tesseract":
        import pytesseract  # lazy import

        # Point pytesseract at the exact binary we found during setup rather
        # than relying on PATH — some installers (notably UB-Mannheim's
        # Windows installer run silently via winget) install successfully
        # without registering on PATH, so PATH alone isn't reliable here.
        config = app_config.load_config()
        tesseract_path = config.get("tesseract_path")
        if tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path

        text = pytesseract.image_to_string(image)
        return text.strip()

    if engine == "easyocr":
        import numpy as np  # available transitively via easyocr/pymupdf stack

        reader = _get_easyocr_reader()
        image_array = np.array(image.convert("RGB"))
        detections = reader.readtext(image_array, detail=0, paragraph=True)
        return "\n\n".join(detections).strip()

    raise ValueError(f"Unknown OCR engine: {engine}")


def _convert_pdf_with_ocr(path: Path, engine: str) -> str:
    import pymupdf as fitz  # PyMuPDF's modern import name (fitz is deprecated)

    page_sections: List[str] = []
    zoom = _OCR_RENDER_DPI / 72  # PyMuPDF's default page resolution is 72 DPI
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(str(path)) as doc:
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix)

            from PIL import Image

            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            page_text = _ocr_pil_image(image, engine)
            if page_text:
                page_sections.append(f"\n\n<!-- Page {page_index + 1} (OCR) -->\n\n{page_text}")

    full_markdown = "".join(page_sections).strip()
    if not full_markdown:
        raise ValueError("OCR produced no extractable text")
    return full_markdown


def _convert_image_with_ocr(path: Path, engine: str) -> str:
    from PIL import Image

    with Image.open(path) as image:
        text = _ocr_pil_image(image, engine)

    if not text:
        raise ValueError("OCR produced no extractable text")
    return f"# {path.stem}\n\n{text}"


def _convert_pdf(path: Path, result: ConversionResult, detected: file_types.DetectedType) -> str:
    """Runs the PDF fallback cascade, recording attempts on result.

    When the file is flagged as scanned/mixed, OCR (if enabled) is tried
    FIRST, since pymupdf4llm/pdfplumber will return little or nothing for
    image-only pages. Otherwise the normal text-extraction cascade runs,
    with OCR skipped entirely (fastest path for ordinary text PDFs).
    """
    stages = []

    ocr_enabled, ocr_engine = _ocr_settings()
    if detected.needs_ocr:
        if ocr_enabled and ocr_engine:
            stages.append((f"ocr-{ocr_engine}", lambda p: _convert_pdf_with_ocr(p, ocr_engine)))
        else:
            result.warning = (
                "This file appears to be scanned/image-based, but OCR is not enabled. "
                "Extracted content may be empty or incomplete. Enable OCR from the app "
                "to read scanned pages."
            )

    stages += [
        ("pymupdf4llm", _convert_pdf_with_pymupdf4llm),
        ("pdfplumber", _convert_pdf_with_pdfplumber),
        ("markitdown", _convert_with_markitdown),
    ]

    last_exception: Optional[Exception] = None
    last_traceback: Optional[str] = None

    for engine_name, engine_fn in stages:
        result.engine_attempts.append(engine_name)
        try:
            markdown_text = engine_fn(path)
            result.engine_used = engine_name
            return markdown_text
        except Exception as exc:  # noqa: BLE001 - deliberately broad; isolate per engine
            last_exception = exc
            last_traceback = _log_attempt_failure(engine_name, exc)
            continue

    assert last_exception is not None
    raise RuntimeError(
        f"All PDF extraction engines failed for {path.name}: {last_exception}"
    ) from last_exception


def convert_file(source_path: Path, input_root: Path, output_root: Path) -> ConversionResult:
    """
    Converts a single file to Markdown, mirroring its position relative to
    input_root under output_root. Never raises — all failures are captured
    on the returned ConversionResult so batch processing can continue.
    """
    result = ConversionResult(source_path=source_path)

    try:
        relative_path = source_path.relative_to(input_root)
    except ValueError:
        relative_path = Path(source_path.name)

    destination = (output_root / relative_path).with_suffix(".md")

    try:
        extension = source_path.suffix.lower()
        detected = file_types.detect_type(source_path)
        result.detected_type_label = detected.label

        if extension == _PDF_EXTENSION:
            markdown_text = _convert_pdf(source_path, result, detected)

        elif detected.category == "image":
            ocr_enabled, ocr_engine = _ocr_settings()
            if ocr_enabled and ocr_engine:
                result.engine_attempts.append(f"ocr-{ocr_engine}")
                markdown_text = _convert_image_with_ocr(source_path, ocr_engine)
                result.engine_used = f"ocr-{ocr_engine}"
            else:
                result.warning = (
                    "OCR is not enabled, so no text could be extracted from this image. "
                    "Enable OCR from the app to read image files."
                )
                result.engine_attempts.append("markitdown")
                markdown_text = _convert_with_markitdown(source_path)
                result.engine_used = "markitdown"

        elif extension in _MARKITDOWN_DIRECT_EXTENSIONS:
            result.engine_attempts.append("markitdown")
            markdown_text = _convert_with_markitdown(source_path)
            result.engine_used = "markitdown"
        else:
            # Unknown extension: still give MarkItDown a chance, since it
            # auto-detects a wide range of formats by content, not just suffix.
            result.engine_attempts.append("markitdown")
            markdown_text = _convert_with_markitdown(source_path)
            result.engine_used = "markitdown"

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown_text, encoding="utf-8")

        result.output_path = destination
        result.markdown = markdown_text
        result.success = True

    except Exception as exc:  # noqa: BLE001 - top-level safe-harbor for the whole file
        result.success = False
        result.error_message = str(exc)
        result.error_traceback = traceback.format_exc()
        logger.error("Failed to convert %s: %s", source_path, exc)

    return result
