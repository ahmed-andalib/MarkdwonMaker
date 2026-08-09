"""
parser_selector.py — Decides whether a PDF should route to the fast,
lightweight cascade (pymupdf4llm -> pdfplumber -> markitdown) or to
Docling, a heavier ML-based parser with real figure/table/formula support.

Design principle: confidence-ordered signals, not an invented point score.
With no labeled dataset to calibrate weights against, a blended numeric
score creates false precision. Instead, each signal is checked in order of
how certain we actually are about it, and the first one that fires decides
the route immediately. Anything that gets through all of them un-flagged
still isn't fully trusted — converter.py runs a cheap post-hoc quality
check on the fast cascade's own output and escalates to Docling if that
looks weak, so a wrong "easy" call here is self-correcting rather than a
permanent mistake.

This module is a pure function of the PDF's own content — it has no
knowledge of whether Docling is installed or enabled. That gating lives in
converter.py, matching how OCR enablement is handled there.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

ProgressCallback = Callable[[str, str], None]  # (stage, message) -> None

_SAMPLE_PAGE_LIMIT = 5

# A raster image or a cluster of vector-drawn shapes has to span at least
# this many points (144pt = 2 inches at 72pt/inch) in both dimensions to
# count as "a real figure" rather than an icon, logo, or table ruling.
_MIN_FIGURE_SIZE_PT = 144
_MIN_VECTOR_ITEMS_PER_FIGURE = 5

# A table is "irregular" if more than this fraction of its rows disagree
# with the most common column count — a proxy for merged cells or a
# borderless table pdfplumber is likely to have mangled.
_TABLE_IRREGULAR_ROW_FRACTION = 0.30
_MIN_ROWS_TO_JUDGE_TABLE = 3

# Common TeX/LaTeX math font family name fragments. Checked as substrings
# against the whole font-info tuple rather than a specific tuple index, to
# stay robust against minor API differences across PyMuPDF versions.
_MATH_FONT_MARKERS = ("CMMI", "CMSY", "CMEX", "CMR", "MSAM", "MSBM", "LMMI", "LMSY", "EUFM")

# Post-hoc output-quality thresholds. Self-referential where possible
# (compared against the PDF's own raw text) rather than invented universal
# constants, since those would need calibration we don't have data for.
_MIN_OUTPUT_RATIO_VS_RAW_TEXT = 0.5
_MAX_SINGLE_CHAR_WORD_RATIO = 0.15
_MAX_DUPLICATE_LINE_RATIO = 0.25


def _noop_report(stage: str, message: str) -> None:
    pass


@dataclass
class ParserDecision:
    engine: str  # "fast_cascade" | "docling"
    reason: str  # human-readable, safe to show directly in the UI
    triggered_by: Optional[str] = None  # "figures" | "tables" | "math_fonts" | None
    sample_raw_text_length: int = 0  # reused by the post-hoc quality check


def _page_has_raster_figure(page) -> bool:
    for img_info in page.get_images(full=True):
        try:
            bbox = page.get_image_bbox(img_info)
        except ValueError:
            continue
        if bbox.width >= _MIN_FIGURE_SIZE_PT and bbox.height >= _MIN_FIGURE_SIZE_PT:
            return True
    return False


def _page_has_vector_figure(page) -> bool:
    # Many academic charts/diagrams are drawn with PDF drawing operators
    # rather than embedded as raster images — checking only get_images()
    # would miss these entirely. A real chart is typically emitted as one
    # cohesive drawing object (or a small number of them) with genuine
    # width, height, and enough sub-path segments to be a real shape.
    #
    # Checking EACH drawing individually — rather than aggregating bounding
    # boxes across every drawing on the page, which the original version of
    # this function did — matters a lot in practice: a page full of
    # scattered decorative elements (section-divider rules, underlines,
    # icon glyphs — common in resume/CV templates) can combine into a huge
    # page-spanning bounding box despite each one individually being a
    # thin, trivial line. This was confirmed as a real false-positive
    # against an actual resume PDF: 73 separate divider lines, none wider
    # than ~1.5pt in one dimension, whose combined span looked like a
    # large figure spanning most of the page.
    try:
        drawings = page.get_drawings()
    except Exception:
        return False

    for drawing in drawings:
        rect = drawing.get("rect")
        if rect is None:
            continue
        items = drawing.get("items", [])
        if (
            rect.width >= _MIN_FIGURE_SIZE_PT
            and rect.height >= _MIN_FIGURE_SIZE_PT
            and len(items) >= _MIN_VECTOR_ITEMS_PER_FIGURE
        ):
            return True
    return False


def _has_meaningful_figure(doc, sample_count: int) -> bool:
    for page_index in range(sample_count):
        page = doc.load_page(page_index)
        if _page_has_raster_figure(page) or _page_has_vector_figure(page):
            return True
    return False


def _tables_look_irregular(path: Path, sample_count: int) -> bool:
    try:
        import pdfplumber  # lazy import; already a required dependency
    except ImportError:
        return False

    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:sample_count]:
                try:
                    tables = page.extract_tables()
                except Exception:
                    continue
                for table in tables:
                    row_lengths = [len(row) for row in table if row]
                    if len(row_lengths) < _MIN_ROWS_TO_JUDGE_TABLE:
                        continue
                    modal_length = max(set(row_lengths), key=row_lengths.count)
                    irregular_rows = sum(1 for length in row_lengths if length != modal_length)
                    if irregular_rows / len(row_lengths) > _TABLE_IRREGULAR_ROW_FRACTION:
                        return True
    except Exception:
        return False
    return False


def _math_fonts_present(doc, sample_count: int) -> bool:
    for page_index in range(sample_count):
        page = doc.load_page(page_index)
        try:
            fonts = page.get_fonts(full=True)
        except Exception:
            continue
        for font in fonts:
            font_repr = " ".join(str(part) for part in font).upper()
            if any(marker in font_repr for marker in _MATH_FONT_MARKERS):
                return True
    return False


def select_parser(path: Path, report: ProgressCallback = _noop_report) -> ParserDecision:
    """
    Pre-flight routing decision for a single PDF. Checks signals in
    confidence order and returns as soon as one fires. Never raises — on
    any internal error, defaults to the fast cascade, which is always
    available.
    """
    try:
        import pymupdf as fitz  # modern import name; fitz is deprecated
    except ImportError:
        report("analyzing", "Analyzer unavailable — using fast cascade")
        return ParserDecision(engine="fast_cascade", reason="PyMuPDF not available for analysis")

    try:
        with fitz.open(str(path)) as doc:
            sample_count = min(doc.page_count, _SAMPLE_PAGE_LIMIT)
            if sample_count == 0:
                return ParserDecision(engine="fast_cascade", reason="Empty PDF")

            raw_text_length = sum(
                len(doc.load_page(i).get_text("text")) for i in range(sample_count)
            )

            report("analyzing", "Checking for figures…")
            if _has_meaningful_figure(doc, sample_count):
                report("decided", "Figures detected — routing to Docling")
                return ParserDecision(
                    engine="docling", reason="Contains figures or diagrams",
                    triggered_by="figures", sample_raw_text_length=raw_text_length,
                )

            report("analyzing", "Checking table structure…")
            if _tables_look_irregular(path, sample_count):
                report("decided", "Irregular tables detected — routing to Docling")
                return ParserDecision(
                    engine="docling", reason="Complex or irregular tables",
                    triggered_by="tables", sample_raw_text_length=raw_text_length,
                )

            report("analyzing", "Checking for mathematical notation…")
            if _math_fonts_present(doc, sample_count):
                report("decided", "Math-heavy fonts detected — routing to Docling")
                return ParserDecision(
                    engine="docling", reason="Mathematical notation present",
                    triggered_by="math_fonts", sample_raw_text_length=raw_text_length,
                )

            report("decided", "No high-risk signals found — using fast cascade")
            return ParserDecision(
                engine="fast_cascade", reason="No figures, tables, or math flagged as high-risk",
                sample_raw_text_length=raw_text_length,
            )

    except Exception as exc:  # noqa: BLE001 - analysis must never break conversion
        report("analyzing", f"Analysis failed ({exc}) — defaulting to fast cascade")
        return ParserDecision(engine="fast_cascade", reason=f"Analysis error: {exc}")


def output_quality_looks_weak(markdown_text: str, sample_raw_text_length: int) -> Optional[str]:
    """
    Post-hoc safety net, run on the fast cascade's own output for PDFs the
    pre-flight check considered low-risk. Returns a short human-readable
    reason if the output looks unreliable (caller should escalate to
    Docling), or None if it looks fine.

    Checks are self-referential (compared against this PDF's own raw text)
    where possible, rather than invented universal density constants,
    since there's no labeled data to calibrate a fixed threshold against.
    """
    if not markdown_text or not markdown_text.strip():
        return "empty output"

    if sample_raw_text_length > 200 and len(markdown_text) < sample_raw_text_length * _MIN_OUTPUT_RATIO_VS_RAW_TEXT:
        return "markdown output is much shorter than the PDF's own raw text"

    words = markdown_text.split()
    if len(words) >= 20:
        single_char_words = sum(1 for w in words if len(w) == 1 and w.isalnum())
        if single_char_words / len(words) > _MAX_SINGLE_CHAR_WORD_RATIO:
            return "high ratio of single-character tokens (likely garbled extraction)"

    lines = [line.strip() for line in markdown_text.splitlines() if line.strip()]
    if len(lines) >= 10:
        duplicate_count = len(lines) - len(set(lines))
        if duplicate_count / len(lines) > _MAX_DUPLICATE_LINE_RATIO:
            return "high ratio of duplicate lines (possible column-merge artifact)"

    return None