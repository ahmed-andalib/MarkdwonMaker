[README.md](https://github.com/user-attachments/files/30812601/README.md)
# MarkdwonMaker
quick and easy markdown maker using Python
# Markdown Maker

A lightweight, cross-platform desktop app that batch-converts PDF, DOCX,
XLSX, PPTX, CSV, HTML, TXT, and image files into clean Markdown — tuned to
handle dense, multi-column academic research PDFs without scrambling
reading order or losing tables, with optional OCR for scanned documents
and photos.

## Requirements

- Python 3.9+
- **Linux only:** the `tkinter` GUI toolkit itself is not installed by pip.
  Install it via your system package manager first, e.g.:
  - Debian/Ubuntu: `sudo apt install python3-tk`
  - Fedora: `sudo dnf install python3-tkinter`
  - Arch: `sudo pacman -S tk`
  (Windows and macOS installers from python.org already include Tk.)

Everything else (`markitdown`, `pymupdf4llm`, `pdfplumber`, `tkinterdnd2`,
`openpyxl`) is installed automatically on first launch if missing — the app
asks for permission before doing so. OCR support is entirely separate and
optional (see below).

## Running

```bash
python main.py
```

On first run:

1. If any core package is missing, a dialog asks: *"Missing components
   detected. Do you want to download required modules automatically?"*
   Click **Yes** to let it `pip install` them for you (a progress window
   shows live pip output), or **No** to see the manual install command.
2. You're then asked: *"Would you like to enable OCR support for scanned
   documents and images?"* This is optional and only asked once.
   - **Yes** → choose **Tesseract** (fast, but needs a native program
     installed alongside the Python package) or **EasyOCR** (installs
     entirely through pip, larger first-time download, slower per page).
   - **No** → the app runs normally; scanned/image files will still be
     queued and converted, just without extracted text, and each one is
     flagged with a warning in the Detail column.

You can change this anytime from **Configure OCR…** in the app's toolbar,
which reruns the same prompt.

Alternatively, install core requirements up front:

```bash
pip install -r requirements.txt
python main.py
```

### How OCR gets installed

- **EasyOCR** is a single `pip install easyocr` — no other setup needed.
- **Tesseract** needs two things: the `pytesseract` pip package (always
  automatable) and the actual `tesseract` OCR engine, which is a native
  program pip cannot install. Markdown Maker detects your OS —
  distinguishing Windows 10 vs 11 by build number, resolving your macOS
  version and codename, and reading `/etc/os-release` to identify your
  Linux distribution (Ubuntu, Fedora, Arch, openSUSE, etc.) — then runs
  the matching package manager command automatically:

  | Platform | Command used |
  |---|---|
  | Windows (winget available) | `winget install --id UB-Mannheim.TesseractOCR -e --silent` |
  | Windows (choco available) | `choco install tesseract -y` |
  | macOS (Homebrew available) | `brew install tesseract` |
  | Ubuntu/Debian | `apt-get install -y tesseract-ocr` (via `pkexec` if no root shell) |
  | Fedora/RHEL | `dnf install -y tesseract` |
  | Arch/Manjaro | `pacman -S --noconfirm tesseract` |
  | openSUSE | `zypper install tesseract-ocr` |

  If none of these apply (no supported package manager found, or
  elevation isn't available), Markdown Maker shows the exact manual
  command for your platform instead of failing silently.

## Using the app

1. **Add files** via *Add Files…*, *Add Folder…* (recurses into
   subfolders), or by dragging files/folders directly onto the drop zone.
2. Every file appears in the queue **immediately** with its detected
   **Type** — e.g. `PDF — Text`, `PDF — Scanned (OCR)`, `Word Document`,
   `Image File` — filled in a moment later by a background check so large
   batches don't stall the UI. Files needing OCR are highlighted and
   tagged so you know at a glance which ones depend on OCR being enabled.
3. **Choose Output Folder…** — required before starting. Converted files
   mirror the input folder structure under this output folder (e.g.
   `papers/2024/study.pdf` → `<output>/2024/study.md`).
4. **Start Conversion** — runs in a background thread pool sized to your
   CPU core count (capped at 8), so the UI stays responsive throughout.
5. Watch per-file status update live (`Queued` → `Processing` →
   `Done`/`Error`), an overall progress bar, and a "Processing file X of
   Y" counter.
6. Any failures — or successes with caveats, like a scanned file processed
   without OCR enabled — show up in the **Diagnostics** pane and the
   Detail column. Failures are also written to `_conversion_errors.log`
   inside the output folder with full tracebacks. A single corrupt file
   never stops the batch.

## Architecture

| Module | Responsibility |
|---|---|
| `main.py` | Entry point; runs core dependency bootstrap, then the optional OCR setup prompt, then launches the UI. |
| `bootstrap.py` | Mandatory core dependency check/install via pip. Separately, the opt-in OCR flow: asks once, lets the user pick an engine, and installs it (persisted via `app_config`). |
| `platform_utils.py` | Detects the host OS/distro and available package manager, and builds the right Tesseract install command (or manual instructions if none apply). |
| `app_config.py` | Persists the OCR decision to `~/.markdown_maker/config.json` so it's only asked once. |
| `file_types.py` | Cheap per-file type detection (text vs scanned PDF, image, document type) used both for the UI's Type column and to decide whether `converter.py` should attempt OCR. |
| `converter.py` | Per-file conversion orchestrator. Non-PDF files go through `MarkItDown()`. PDFs flagged as scanned/mixed try OCR first (if enabled), then fall back through `pymupdf4llm` → `pdfplumber` → `MarkItDown()`. Standalone images go straight to OCR if enabled. Every stage and every file is independently fault-isolated. |
| `ui.py` | Tkinter/ttk layout, drag-and-drop (via `tkinterdnd2`), background type detection, and thread orchestration. A `ThreadPoolExecutor` runs conversions off the GUI thread; workers communicate back to the main thread exclusively via a `queue.Queue`, polled every 100ms with `root.after()`. |

## Why the PDF fallback cascade?

No single extraction library is reliably correct across the full range of
academic PDF layouts:

- For **text-based** PDFs, `pymupdf4llm` is tried first — it understands
  multi-column reading order and emits headings/tables as Markdown
  directly. If it fails or returns implausibly little text, the app falls
  back to a manual `pdfplumber` pass (layout-aware text plus explicit
  `extract_tables()` converted into GitHub-Flavored Markdown), then to
  `MarkItDown()` as a last resort.
- For PDFs **flagged as scanned or mixed** (detected via a quick sample of
  the first few pages — meaningful text vs. embedded images with none),
  OCR runs *first* if enabled, since the other engines have nothing to
  extract from an image-only page. If OCR is disabled, the file still
  runs through the same cascade and reports how much (if anything) could
  be recovered, with a clear warning rather than a silent, misleadingly
  "successful" empty result.

## Known limitations

- OCR accuracy depends on scan quality; skewed or low-resolution scans
  will produce noisier text with either engine.
- Complex inline mathematical notation is preserved only as well as the
  underlying engine renders it; this is not a full LaTeX reconstruction.
- OCR is meaningfully slower than text extraction — large batches of
  scanned documents will take noticeably longer than the same batch of
  text-based files.
- "Cancel" stops any not-yet-started files immediately but lets
  already-running conversions finish (Python threads cannot be forcibly
  killed mid-execution).
- Tesseract's automated install requires a supported package manager
  (`winget`/`choco`, `brew`, `apt`/`dnf`/`pacman`/`zypper`) and, on Linux,
  either running as root already or a working `pkexec` policy agent. If
  neither is available, Markdown Maker shows manual install instructions
  instead.

## Next phase:
- create a web version
- create an desktop installer
