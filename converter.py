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

import html
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import app_config
import file_types
import parser_selector

logger = logging.getLogger("markdown_maker.converter")

# Docling's layout model uses torch.compile for speed, which requires a
# native C++ compiler (cl.exe on Windows via MSVC Build Tools). Most users
# won't have that installed, and installing it just for this optional
# feature would be a multi-GB detour. Disabling compilation forces PyTorch
# into plain eager-mode execution instead — a bit slower per page, but
# reliable with zero extra system dependencies. This has to be set before
# torch is ever imported anywhere in the process (Docling imports it
# transitively), so it's set here at module load rather than only right
# before the lazy Docling import.
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# Rendering DPI used when rasterizing PDF pages for OCR. Higher is more
# accurate but slower; 200 is a good balance for typical scanned documents.
_OCR_RENDER_DPI = 200

# Common short English words that could legitimately precede a real "fi…"
# word (e.g. "The first", "a final") — used to guard the ligature-repair
# below against merging two genuine words together.
_COMMON_SHORT_WORDS_BEFORE_FI = {
    "the", "a", "an", "in", "on", "at", "to", "of", "is", "as", "by", "or",
    "and", "be", "we", "he", "she", "it", "if", "so", "no", "do", "up", "my",
    "his", "her", "its", "for", "not", "but", "you", "was", "are", "can",
    "this", "that", "with", "from", "will", "when", "than", "then",
}


def _fix_docling_fi_ligature(text: str) -> str:
    """
    Some fonts render the "fi" letter-pair as a single ligature glyph, and
    Docling's text extraction can turn that into "fi" with a spurious space
    injected immediately after it — and, when the ligature falls inside a
    word rather than at its start, another spurious space immediately
    before it too (e.g. "arti fi cial" instead of "artificial", "Cran fi eld"
    instead of "Cranfield"). Confirmed as a real, systematic defect against
    an actual academic PDF — every occurrence of "fi" was affected the same
    way throughout the whole document.

    Two-step repair:
    1. The space right after "fi" is spurious in every observed case —
       always safe to remove.
    2. The space right before "fi" is only spurious when the word-fragment
       before it isn't a genuine standalone word (e.g. "arti" isn't a real
       word, so "arti fi cial" should merge). Guarded by a short blocklist
       of common words so real word boundaries like "The first" or
       "a final" are never merged together.
    """
    text = re.sub(r"\b([Ff]i) (?=[a-z])", r"\1", text)

    def _maybe_merge(match: "re.Match") -> str:
        word_before, fi_continuation = match.group(1), match.group(2)
        if word_before.lower() in _COMMON_SHORT_WORDS_BEFORE_FI:
            return match.group(0)
        return word_before + fi_continuation

    text = re.sub(r"\b([A-Za-z]+) ([Ff]i[a-z]+)", _maybe_merge, text)
    return text

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


_MAX_LOGGED_ERROR_LENGTH = 500


def _log_attempt_failure(engine_name: str, exc: Exception) -> str:
    tb = traceback.format_exc()
    # Defensive truncation — some libraries (Docling included, when a
    # dependency like a native compiler is missing) can raise exceptions
    # with very long, repeated error text. Truncating what we print/log
    # keeps the console and diagnostics pane readable regardless of what
    # any given engine's failure message looks like.
    error_text = str(exc)
    if len(error_text) > _MAX_LOGGED_ERROR_LENGTH:
        error_text = error_text[:_MAX_LOGGED_ERROR_LENGTH] + "… [truncated]"
    logger.warning("Engine '%s' failed for a file: %s", engine_name, error_text)
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


def _docling_enabled() -> bool:
    return bool(app_config.load_config().get("docling_enabled"))


# DocumentConverter() loads layout/table-structure models and is expensive
# to construct — cache a single instance per process, same pattern used for
# the EasyOCR reader, guarded by a lock since multiple worker threads may
# need it concurrently.
_docling_converter = None
_docling_lock = None


def _get_docling_converter():
    global _docling_converter, _docling_lock
    import threading

    if _docling_lock is None:
        _docling_lock = threading.Lock()

    with _docling_lock:
        if _docling_converter is None:
            from docling.document_converter import DocumentConverter  # lazy import

            _docling_converter = DocumentConverter()
        return _docling_converter


def _convert_pdf_with_docling(path: Path, report) -> str:
    try:
        from docling.document_converter import DocumentConverter  # noqa: F401 - import check
    except ImportError as exc:
        raise ImportError(
            "Docling is not installed — enable advanced parsing from the app to install it"
        ) from exc

    report("converting", "Running advanced parser (Docling)…")
    docling_converter = _get_docling_converter()
    result = docling_converter.convert(str(path))
    markdown_text = result.document.export_to_markdown()

    if not markdown_text or not markdown_text.strip():
        raise ValueError("Docling returned empty content")

    # Docling's markdown export leaves HTML entities (e.g. "&amp;" instead
    # of "&") in the text rather than decoding them. Harmless for LLM
    # ingestion, which handles entities fine, but visibly wrong if the
    # markdown is ever rendered or exact-matched against plain text —
    # cheap, safe fix with no downside, so just always do it.
    markdown_text = html.unescape(markdown_text)

    # See _fix_docling_fi_ligature's docstring — repairs a confirmed
    # systematic "fi" ligature corruption in Docling's text extraction.
    markdown_text = _fix_docling_fi_ligature(markdown_text)

    return markdown_text


def _convert_pdf(path: Path, result: ConversionResult, detected: file_types.DetectedType,
                  progress_callback: Optional[Callable[[str, str], None]] = None) -> str:
    """Runs the PDF fallback cascade, recording attempts on result.

    When the file is flagged as scanned/mixed, OCR (if enabled) is tried
    FIRST, since pymupdf4llm/pdfplumber will return little or nothing for
    image-only pages — this already routes scanned pages through the most
    aggressive extraction available, so the Docling complexity routing
    below is scoped to text-bearing PDFs only, to keep the two systems from
    overlapping in this first version.

    For text-bearing PDFs, if advanced parsing (Docling) is enabled,
    parser_selector.select_parser() decides up front whether this specific
    file looks complex enough (real figures, irregular tables, heavy math)
    to warrant it. If the fast cascade runs instead and its own output
    later looks weak, it's escalated to Docling automatically as a safety
    net — the pre-flight decision doesn't have to be perfectly calibrated
    if a wrong "easy" call gets caught and corrected right after.
    """
    report = progress_callback or (lambda stage, message: None)
    stages = []
    decision: Optional[parser_selector.ParserDecision] = None

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
    elif _docling_enabled():
        decision = parser_selector.select_parser(path, report=report)
        if decision.engine == "docling":
            stages.append(("docling", lambda p: _convert_pdf_with_docling(p, report)))

    stages += [
        ("pymupdf4llm", _convert_pdf_with_pymupdf4llm),
        ("pdfplumber", _convert_pdf_with_pdfplumber),
        ("markitdown", _convert_with_markitdown),
    ]

    last_exception: Optional[Exception] = None
    last_traceback: Optional[str] = None
    fast_cascade_engines = ("pymupdf4llm", "pdfplumber", "markitdown")

    for engine_name, engine_fn in stages:
        result.engine_attempts.append(engine_name)
        try:
            markdown_text = engine_fn(path)
            result.engine_used = engine_name

            if engine_name != "docling" and "docling" in result.engine_attempts[:-1]:
                # Docling was the chosen route for this file (real figures,
                # complex tables, or math detected) but failed, and we fell
                # through to a fast-cascade engine instead. Surface this
                # rather than silently succeeding — the fallback content is
                # usually fine for plain text, but figures/complex tables
                # this file was flagged for may not be fully captured.
                report("fallback", f"Advanced parser failed — used {engine_name} instead")
                result.warning = (
                    "Advanced parsing (Docling) was selected for this file but failed, "
                    f"so the standard parser ({engine_name}) was used instead. Figures, "
                    "complex tables, or formulas may not be fully captured. Check the "
                    "diagnostics log for the underlying error."
                )

            # Post-hoc quality gate: only applies when parser_selector chose
            # the fast cascade in the first place and it just produced a
            # result via one of the fast engines. Docling's own output, and
            # the OCR path, aren't re-checked here.
            if (
                decision is not None
                and decision.engine == "fast_cascade"
                and engine_name in fast_cascade_engines
                and _docling_enabled()
            ):
                report("checking", "Verifying output quality…")
                weak_reason = parser_selector.output_quality_looks_weak(
                    markdown_text, decision.sample_raw_text_length
                )
                if weak_reason:
                    report("escalating", f"Output looked weak ({weak_reason}) — retrying with advanced parser…")
                    try:
                        docling_text = _convert_pdf_with_docling(path, report)
                        result.engine_used = "docling (escalated)"
                        result.engine_attempts.append("docling (escalated)")
                        return docling_text
                    except Exception as docling_exc:  # noqa: BLE001 - escalation is best-effort
                        result.warning = (
                            f"Fast extraction looked weak ({weak_reason}) and the advanced-parser "
                            f"retry also failed ({docling_exc}); using the original result."
                        )
                        logger.warning("Docling escalation failed for %s: %s", path.name, docling_exc)
                        # fall through — keep the original markdown_text below

            return markdown_text
        except Exception as exc:  # noqa: BLE001 - deliberately broad; isolate per engine
            last_exception = exc
            last_traceback = _log_attempt_failure(engine_name, exc)
            continue

    assert last_exception is not None
    raise RuntimeError(
        f"All PDF extraction engines failed for {path.name}: {last_exception}"
    ) from last_exception


def convert_file(source_path: Path, input_root: Path, output_root: Path,
                  progress_callback: Optional[Callable[[str, str], None]] = None) -> ConversionResult:
    """
    Converts a single file to Markdown, mirroring its position relative to
    input_root under output_root. Never raises — all failures are captured
    on the returned ConversionResult so batch processing can continue.

    progress_callback, if given, is called as callback(stage, message) at
    each analysis/conversion stage for PDFs — used by the UI to show live,
    non-blocking status in the queue list rather than a popup dialog.
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
            markdown_text = _convert_pdf(source_path, result, detected, progress_callback)

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
