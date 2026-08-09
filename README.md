# Markdown Maker

A lightweight, cross-platform desktop app that batch-converts PDF, DOCX,
XLSX, PPTX, CSV, HTML, TXT, and image files into clean Markdown — tuned to
handle dense, multi-column academic research PDFs without scrambling
reading order or losing tables, with optional OCR for scanned documents
and optional advanced parsing (via Docling) for PDFs with real figures,
complex tables, or heavy math notation.

## Requirements

- Python 3.9+
- **Linux only:** the `tkinter` GUI toolkit itself is not installed by pip.
  Install it via your system package manager first, e.g.:
  - Debian/Ubuntu: `sudo apt install python3-tk`
  - Fedora: `sudo dnf install python3-tkinter`
  - Arch: `sudo pacman -S tk`
  (Windows and macOS installers from python.org already include Tk.)

Everything else (`markitdown`, `pymupdf4llm`, `pdfplumber`, `tkinterdnd2`,
`openpyxl`, `sv-ttk`) is installed automatically on first launch if
missing — the app asks for permission before doing so. OCR and advanced
parsing are both entirely separate, optional features (see below).

## Running

```bash
python main.py
```

On first run:

1. If any core package is missing, a dialog asks: *"Missing components
   detected. Do you want to download required modules automatically?"*
   Click **Yes** to let it `pip install` them for you.
2. You're asked whether to enable **OCR** for scanned documents/images
   (optional, choice of Tesseract or EasyOCR).
3. You're asked whether to enable **advanced PDF parsing** via Docling
   (optional — see below).

Both prompts only appear once; revisit them anytime from **Configure
OCR…** and **Configure Advanced Parsing…** in the app's toolbar.

Alternatively, install core requirements up front:

```bash
pip install -r requirements.txt
python main.py
```

## Advanced PDF parsing (Docling) — new

Most PDFs are handled well by the fast built-in cascade (see below). But
some — papers with real figures/charts, irregular or borderless tables,
heavy mathematical notation — benefit from a heavier, ML-based parser.
Markdown Maker can use [Docling](https://github.com/docling-project/docling)
for exactly those files, automatically, while keeping everything else on
the fast path:

- **Per-file routing, not a global switch.** Each PDF is analyzed cheaply
  before conversion (checking for real figures, irregular tables, and
  math-heavy fonts) and only routed to Docling if it actually looks like
  it needs it. A plain text-only PDF never pays Docling's overhead.
- **A safety net for wrong calls.** If a file was *not* flagged but the
  fast cascade's own output looks weak afterward (garbled text, oddly
  short, duplicated lines), it's automatically retried through Docling —
  so a wrong "this looks easy" call gets caught and corrected rather than
  silently shipping bad output.
- **Fully interactive, non-blocking.** While a file is being analyzed and
  converted, you can watch it happen live in the queue — "Checking for
  figures…", "Figures detected — routing to Docling…", "Verifying output
  quality…" — right in the Detail column and the Diagnostics pane. No
  popups, nothing blocks the rest of the batch.
- **Off by default, and a large download when enabled** (~1–2GB, since it
  includes layout-analysis and table-structure models) — enable it from
  **Configure Advanced Parsing…** when you're ready, not urgent to do on
  first run.
- **Graceful if it fails or isn't installed.** If Docling errors out on a
  given file, Markdown Maker falls back to the normal cascade automatically
  and flags it in the Detail column, rather than failing the whole file.

## Using the app

1. **Add files** via *Add Files…*, *Add Folder…* (recurses into
   subfolders), or by dragging files/folders directly onto the drop zone.
2. Every file appears in the queue **immediately** with its detected
   **Type** — e.g. `PDF — Text`, `PDF — Scanned (OCR)`, `Word Document`,
   `Image File` — filled in a moment later by a background check so large
   batches don't stall the UI. Files needing OCR are highlighted.
3. **Remove files from the queue** anytime before starting: select one or
   more rows (ctrl/shift-click) and click **Remove Selected**, press
   **Delete**, or right-click for a context menu.
4. **Choose Output Folder…** — required before starting. Converted files
   mirror the input folder structure under this output folder.
5. **Start Conversion** — runs in a background thread pool sized to your
   CPU core count (capped at 8), so the UI stays responsive throughout.
6. Watch per-file status update live (`Queued` → `Processing` →
   `Done`/`Error`), an overall progress bar, and per-file routing/analysis
   messages as they happen.
7. Any failures or caveats show up in the **Diagnostics** pane and the
   Detail column, and are written to `_conversion_errors.log` in the
   output folder. A single corrupt file never stops the batch.
8. Toggle **🌙 Dark Mode / ☀️ Light Mode** anytime in the toolbar — your
   choice is remembered across sessions.

## Architecture

| Module | Responsibility |
|---|---|
| `main.py` | Entry point; runs core dependency bootstrap, then the optional OCR and advanced-parsing setup prompts, then launches the UI. |
| `bootstrap.py` | Mandatory core dependency check/install via pip. Separately, the opt-in OCR and Docling install flows — each asks once, installs what's needed, and persists the choice via `app_config`. |
| `platform_utils.py` | Detects the host OS/distro and available package manager, and builds the right Tesseract install command (or manual instructions if none apply). |
| `app_config.py` | Persists OCR, advanced-parsing, and theme choices to `~/.markdown_maker/config.json`. |
| `file_types.py` | Cheap per-file type detection (text vs scanned PDF, image, document type) for the UI's Type column and to decide whether OCR is needed. |
| `parser_selector.py` | Decides, per PDF, whether to route to Docling or the fast cascade — confidence-ordered structural checks (figures, table irregularity, math fonts), plus a post-hoc output-quality check used as a safety net. |
| `converter.py` | Per-file conversion orchestrator. Non-PDF files go through `MarkItDown()`. PDFs run OCR first if flagged scanned, or Docling first if `parser_selector` flags them as complex, then fall back through `pymupdf4llm` → `pdfplumber` → `MarkItDown()`. Every stage and every file is independently fault-isolated. |
| `ui.py` | Tkinter/ttk layout, drag-and-drop, background type detection, live per-file analysis feedback, and thread orchestration — all communicated back to the main thread via a `queue.Queue` polled with `root.after()`. |

## Why the PDF fallback cascade?

No single extraction library is reliably correct across the full range of
academic PDF layouts. For **text-based** PDFs, `pymupdf4llm` is tried
first, falling back through `pdfplumber` then `MarkItDown()` if it fails
or returns implausibly little text. PDFs **flagged as scanned or mixed**
try OCR first, since the other engines have nothing to extract from an
image-only page. PDFs `parser_selector` flags as **structurally complex**
try Docling first. In every case, if the chosen first engine fails, the
app keeps falling back rather than giving up on the file.

## Known limitations

- OCR and Docling both add real time cost — large batches of scanned or
  complex documents take noticeably longer than plain text-based ones.
- Complex inline mathematical notation is preserved only as well as the
  underlying engine renders it — not a full LaTeX reconstruction, even
  with Docling.
- Neither engine extracts or embeds actual images/figures into the
  output — a detected figure is noted, not recovered as image content.
- `parser_selector`'s routing thresholds are reasoned estimates, not
  calibrated against a labeled dataset (none exists for this yet) — expect
  occasional wrong calls, especially on unusual PDF layouts; report them
  so the heuristics can be tightened.
- Docling's own text extraction has a couple of known quirks that get
  light automatic cleanup on our end (HTML entities, a font-ligature
  artifact) — most other extraction oddities are inherent to the engines
  themselves and outside what this app can fix.
- "Cancel" stops any not-yet-started files immediately but lets
  already-running conversions finish.

---

## TODO

- [ ] **Real image/figure extraction** — currently no engine (including
      Docling) actually extracts and embeds images into the output
      Markdown; a detected figure is only noted, not recovered.
- [ ] **Windows installer** (PyInstaller + Inno Setup) — architecture plan
      exists (see `work_progress.md`) but not started. Open decisions:
      whether to bundle Tesseract into the installer, and whether to pursue
      code signing.
- [ ] **Web-based version** (FastAPI + REST API) — idea only, not started.
      `converter.py`/`file_types.py`/`parser_selector.py` would carry over
      directly; `ui.py`/`bootstrap.py` would need a browser-based rebuild.
- [ ] **Calibrate `parser_selector` against more real-world PDFs** — no
      labeled dataset exists yet; thresholds need ongoing tuning as more
      documents get tested (already caught and fixed one real false-positive
      from resume-template decorative graphics).
- [ ] **Investigate PyMuPDF's occasional silent table loss** — observed on
      a real academic paper (an entire table's content vanished with no
      warning, while Docling captured it correctly on the same file). Not
      yet root-caused or fixed.
- [ ] **Let both OCR engines coexist without a full reinstall** — Tesseract
      and EasyOCR can both be installed today, but switching between them
      still goes through the full "Configure OCR" flow rather than a quick
      toggle; no per-batch engine selection yet.
- [ ] **Extend complexity-based routing beyond PDF** — deliberately scoped
      to PDF only for now, since DOCX/HTML expose real structure directly.
      Revisit only if a real DOCX-fidelity problem shows up in practice.
