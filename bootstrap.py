"""
bootstrap.py — Environment auditing and self-healing dependency installation.

Two distinct flows live here:

1. Core dependencies (mandatory): markitdown, pymupdf4llm, pdfplumber,
   tkinterdnd2, openpyxl. The app cannot run without these, so if they're
   missing the user is asked once and, on agreement, they're pip-installed
   automatically before the UI ever opens.

2. OCR setup (optional, opt-in, asked once and remembered): the user is
   asked whether they want OCR support for scanned PDFs/images at all, and
   if so, which engine — Tesseract (fast, but its OCR engine is a native
   binary pip cannot install) or EasyOCR (pure pip, larger download). The
   decision and outcome are persisted via app_config so this is never asked
   again unless the user re-opens OCR settings from the UI.

This module MUST NOT import any of the heavy/optional third-party libraries
at module load time, since the whole point is to run *before* we know
whether they exist. It only ever touches the Python standard library plus
plain tkinter (which ships with Windows/macOS installers and is available
via the python3-tk package on most Linux distros).
"""

from __future__ import annotations

import importlib.util
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

import app_config
import platform_utils

# ----------------------------------------------------------------------
# Core (mandatory) dependency handling
# ----------------------------------------------------------------------

REQUIRED_PACKAGES: Dict[str, str] = {
    "markitdown": "markitdown[all]",
    "pymupdf4llm": "pymupdf4llm",
    "pdfplumber": "pdfplumber",
    "tkinterdnd2": "tkinterdnd2",
    "openpyxl": "openpyxl",
    "sv_ttk": "sv-ttk",
}

OCR_PACKAGES: Dict[str, str] = {
    "pytesseract": "pytesseract",  # plus the native `tesseract` binary, handled separately
    "easyocr": "easyocr",
}


@dataclass
class DependencyStatus:
    missing_modules: List[str]
    missing_pip_specs: List[str]

    @property
    def all_present(self) -> bool:
        return not self.missing_modules


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def check_dependencies(package_map: Dict[str, str]) -> DependencyStatus:
    missing_modules: List[str] = []
    missing_specs: List[str] = []
    for module_name, pip_spec in package_map.items():
        if not _module_available(module_name):
            missing_modules.append(module_name)
            missing_specs.append(pip_spec)
    return DependencyStatus(missing_modules=missing_modules, missing_pip_specs=missing_specs)


class _CommandProgressWindow:
    """
    A minimal, self-contained Tk window that streams a subprocess's output
    live. Used for both `pip install` (core deps, OCR pip packages) and
    native package manager commands (installing the Tesseract binary).
    """

    def __init__(self, title: str, header: str, subtext: str, command: List[str],
                 manual_fallback_text: Optional[str] = None, parent: Optional[tk.Misc] = None):
        self.command = command
        self.manual_fallback_text = manual_fallback_text
        self._parent = parent

        # If a parent window is given, this is being triggered from inside
        # the already-running main app (e.g. the "Configure OCR…" button) —
        # creating a second independent tk.Tk() root while one is already
        # looping causes windows to silently fail to display. A Toplevel of
        # the real, visible parent is the correct approach there. With no
        # parent (the pre-UI-launch bootstrap flow in main.py), a standalone
        # Tk() root is correct since no other root exists yet.
        self.root = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.root.title(title)
        self.root.geometry("580x380")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_attempt)

        self._closable = False
        self._success = False
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._worker_done = threading.Event()

        ttk.Label(self.root, text=header, font=("Segoe UI", 11, "bold")).pack(
            pady=(14, 4), padx=14, anchor="w"
        )
        ttk.Label(self.root, text=subtext, wraplength=540, justify="left").pack(padx=14, anchor="w")

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=14, pady=10)
        self.progress.start(12)

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.close_button = ttk.Button(
            self.root, text="Please wait…", command=self.root.destroy, state="disabled"
        )
        self.close_button.pack(pady=(0, 12))

        self.root.update_idletasks()
        _bring_window_to_front(self.root)
        if self._parent is not None:
            self.root.grab_set()
        self.root.after(100, self._poll_log_queue)

    def _on_close_attempt(self) -> None:
        if self._closable:
            self.root.destroy()

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                self._append_log(self._log_queue.get_nowait())
        except queue.Empty:
            pass

        if self._worker_done.is_set() and self._log_queue.empty():
            self.progress.stop()
            self.progress["mode"] = "determinate"
            self.progress["value"] = 100
            self._closable = True
            if self._success:
                self.close_button.configure(text="Continue", state="normal")
                self._append_log("\nDone. Click Continue to proceed.\n")
            else:
                self.close_button.configure(text="Close", state="normal")
                fallback = f"\n\n{self.manual_fallback_text}" if self.manual_fallback_text else ""
                self._append_log(f"\nThis step did not complete successfully.{fallback}\n")
            return

        self.root.after(100, self._poll_log_queue)

    def run_and_wait(self) -> bool:
        thread = threading.Thread(target=self._worker, daemon=True)
        thread.start()
        if self._parent is not None:
            self._parent.wait_window(self.root)
        else:
            self.root.mainloop()
        return self._success

    def _worker(self) -> None:
        self._log_queue.put(f"Running: {' '.join(self.command)}\n\n")
        try:
            process = subprocess.Popen(
                self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._log_queue.put(line)
            self._success = process.wait() == 0
        except FileNotFoundError as exc:
            self._log_queue.put(f"\nCommand not found: {exc}\n")
            self._success = False
        except Exception as exc:  # noqa: BLE001 - surface any failure to the log pane
            self._log_queue.put(f"\nUnexpected error: {exc}\n")
            self._success = False
        finally:
            self._worker_done.set()


def _bring_window_to_front(window: tk.Tk) -> None:
    """Forces a Tk window to the foreground with focus.

    Windows in particular can occasionally open a new Tk window behind
    other applications with no taskbar entry, leaving it effectively
    invisible even though the process is running fine. Called on every
    standalone prompt window in this module to avoid that.
    """
    window.update_idletasks()
    window.lift()
    window.attributes("-topmost", True)
    window.after(300, lambda: window.attributes("-topmost", False))
    window.focus_force()


def _run_pip_install(pip_specs: List[str], header: str, subtext: str,
                      parent: Optional[tk.Misc] = None) -> bool:
    command = [sys.executable, "-m", "pip", "install", "--upgrade", *pip_specs]
    window = _CommandProgressWindow(
        title="Markdown Maker — Setting Up", header=header, subtext=subtext, command=command,
        manual_fallback_text=f"Install manually with:\n{' '.join(command)}", parent=parent,
    )
    return window.run_and_wait()


def ensure_dependencies() -> bool:
    """Mandatory core dependency check + install. Returns True if the app is clear to launch."""
    status = check_dependencies(REQUIRED_PACKAGES)
    if status.all_present:
        return True

    probe_root = tk.Tk()
    probe_root.withdraw()
    friendly_list = "\n".join(f"  • {m}" for m in status.missing_modules)
    agreed = messagebox.askyesno(
        title="Markdown Maker — Missing Components",
        message=(
            "Missing components detected. Do you want to download required "
            "modules automatically?\n\n"
            f"The following packages are not installed:\n{friendly_list}\n\n"
            "Markdown Maker can install them now using pip."
        ),
        parent=probe_root,
    )
    probe_root.destroy()

    if not agreed:
        info_root = tk.Tk()
        info_root.withdraw()
        messagebox.showwarning(
            title="Markdown Maker — Cannot Continue",
            message=(
                "Markdown Maker cannot run without these packages.\n\nInstall manually with:\n"
                f"{sys.executable} -m pip install {' '.join(status.missing_pip_specs)}"
            ),
            parent=info_root,
        )
        info_root.destroy()
        return False

    success = _run_pip_install(
        status.missing_pip_specs,
        header="Installing required components…",
        subtext="Markdown Maker needs a few Python packages to run. This only happens once.",
    )
    if not success:
        return False

    return check_dependencies(REQUIRED_PACKAGES).all_present


# ----------------------------------------------------------------------
# Optional OCR setup flow
# ----------------------------------------------------------------------

class _OcrEngineChoiceDialog:
    """A small modal window offering Tesseract / EasyOCR / Skip.

    Deliberately built as its own top-level Tk root rather than a Toplevel
    of a hidden parent — a Toplevel with a withdrawn/hidden master can end
    up without a taskbar entry or window focus on Windows, effectively
    making it invisible even though it's running. A standalone root mirrors
    the same reliable pattern used by _CommandProgressWindow.
    """

    def __init__(self, parent: Optional[tk.Misc] = None):
        self.result: Optional[str] = None  # "tesseract" | "easyocr" | None (skip)
        self._parent = parent

        self.win = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.win.title("Markdown Maker — Choose OCR Engine")
        self.win.geometry("480x340")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._choose_skip)

        ttk.Label(
            self.win, text="Which OCR engine would you like to use?",
            font=("Segoe UI", 11, "bold"),
        ).pack(pady=(16, 8), padx=16, anchor="w")

        tesseract_frame = ttk.LabelFrame(self.win, text="Tesseract", padding=10)
        tesseract_frame.pack(fill="x", padx=16, pady=6)
        ttk.Label(
            tesseract_frame,
            text="Fast and lightweight. Requires installing a small native "
            "program alongside the Python package — Markdown Maker will "
            "attempt this automatically for your system.",
            wraplength=420, justify="left",
        ).pack(anchor="w")
        ttk.Button(tesseract_frame, text="Use Tesseract", command=self._choose_tesseract).pack(
            anchor="e", pady=(8, 0)
        )

        easyocr_frame = ttk.LabelFrame(self.win, text="EasyOCR", padding=10)
        easyocr_frame.pack(fill="x", padx=16, pady=6)
        ttk.Label(
            easyocr_frame,
            text="Installs entirely through pip with no extra software, but "
            "the first-time download is large (several hundred MB) and it "
            "runs slower per page.",
            wraplength=420, justify="left",
        ).pack(anchor="w")
        ttk.Button(easyocr_frame, text="Use EasyOCR", command=self._choose_easyocr).pack(
            anchor="e", pady=(8, 0)
        )

        ttk.Button(self.win, text="Skip OCR for now", command=self._choose_skip).pack(pady=10)

        # Force the window to the foreground and give it focus — without
        # this, Tk windows can occasionally open behind other applications
        # on Windows with no obvious way to find them.
        self.win.update_idletasks()
        self._center_on_screen()
        _bring_window_to_front(self.win)
        if self._parent is not None:
            self.win.grab_set()

    def _center_on_screen(self) -> None:
        width = self.win.winfo_width()
        height = self.win.winfo_height()
        screen_width = self.win.winfo_screenwidth()
        screen_height = self.win.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 3
        self.win.geometry(f"+{x}+{y}")

    def _choose_tesseract(self) -> None:
        self.result = "tesseract"
        self.win.destroy()

    def _choose_easyocr(self) -> None:
        self.result = "easyocr"
        self.win.destroy()

    def _choose_skip(self) -> None:
        self.result = None
        self.win.destroy()

    def show_and_wait(self) -> Optional[str]:
        if self._parent is not None:
            self._parent.wait_window(self.win)
        else:
            self.win.mainloop()
        return self.result


def _install_easyocr(parent: Optional[tk.Misc] = None) -> bool:
    return _run_pip_install(
        [OCR_PACKAGES["easyocr"]],
        header="Installing EasyOCR…",
        subtext="This downloads PyTorch and OCR models — it may take several minutes "
        "on the first run depending on your connection.",
        parent=parent,
    )


def _install_tesseract(parent: Optional[tk.Misc] = None) -> bool:
    # Step 1: the thin Python wrapper, via pip (fast, always automatable).
    pip_ok = _run_pip_install(
        [OCR_PACKAGES["pytesseract"]],
        header="Installing Tesseract Python bindings…",
        subtext="Installing the pytesseract package.",
        parent=parent,
    )
    if not pip_ok:
        return False

    # Step 2: the actual OCR engine binary, which pip cannot install.
    existing_path = platform_utils.find_tesseract_executable()
    if existing_path:
        app_config.save_config({"tesseract_path": existing_path})
        return True

    host = platform_utils.detect_host_platform()
    command = platform_utils.build_tesseract_install_command(host)

    if command is None:
        _show_manual_tesseract_instructions(host, parent=parent)
        return _finalize_tesseract_detection()

    if host.needs_elevation and host.system == "Linux" and shutil.which("pkexec"):
        command = ["pkexec", *command]
    elif host.needs_elevation and host.system == "Linux":
        # No GUI elevation helper available — we cannot silently sudo.
        _show_manual_tesseract_instructions(host, parent=parent)
        return _finalize_tesseract_detection()

    window = _CommandProgressWindow(
        title="Markdown Maker — Installing Tesseract",
        header=f"Installing Tesseract OCR for {host.friendly_name}…",
        subtext="This uses your system's package manager and may prompt for "
        "administrator permission.",
        command=command,
        manual_fallback_text=platform_utils.manual_install_instructions(host),
        parent=parent,
    )
    success = window.run_and_wait()

    if not success or not platform_utils.find_tesseract_executable():
        _show_manual_tesseract_instructions(host, parent=parent)

    return _finalize_tesseract_detection()


def _finalize_tesseract_detection() -> bool:
    """Re-checks for Tesseract (PATH + well-known install paths) and, if
    found, persists its full path so converter.py never has to rely on the
    current process's PATH — which may be stale even when the binary is
    genuinely installed and working."""
    found_path = platform_utils.find_tesseract_executable()
    if found_path:
        app_config.save_config({"tesseract_path": found_path})
        return True
    return False


def _show_manual_tesseract_instructions(host: platform_utils.HostPlatform,
                                         parent: Optional[tk.Misc] = None) -> None:
    message = (
        f"Markdown Maker detected {host.friendly_name} but could not confirm "
        "a working Tesseract install.\n\n"
        "If you just ran the installer, it may have installed successfully "
        "without being added to your system PATH — this is a known behavior "
        "of some silent/unattended installs. Check whether this file exists:\n"
        "  C:\\Program Files\\Tesseract-OCR\\tesseract.exe (Windows)\n\n"
        "If it exists, restart Markdown Maker and it will be found automatically. "
        "If not, install it manually:\n\n" + platform_utils.manual_install_instructions(host) +
        "\n\nYou can re-run OCR setup from within the app once it's installed."
    )
    if parent is not None:
        messagebox.showinfo(title="Markdown Maker — Manual Tesseract Install", message=message, parent=parent)
        return

    info_root = tk.Tk()
    info_root.withdraw()
    _bring_window_to_front(info_root)
    messagebox.showinfo(
        title="Markdown Maker — Manual Tesseract Install", message=message, parent=info_root,
    )
    info_root.destroy()


def ensure_ocr_setup(force_prompt: bool = False, parent: Optional[tk.Misc] = None) -> Dict[str, object]:
    """
    Optional OCR opt-in flow. Only prompts once (persisted via app_config)
    unless force_prompt=True, which the UI uses for a "Configure OCR…" button.

    `parent` should be passed as the main app's root window whenever this is
    called from inside the already-running UI (e.g. "Configure OCR…") rather
    than during the pre-UI-launch bootstrap in main.py. Without it, every
    dialog here creates its own standalone Tk() root, which is correct only
    when no other Tk root exists yet — creating a second independent Tk()
    root while one is already looping causes windows to silently fail to
    display.

    Returns the current config dict with at least ocr_enabled and ocr_engine.
    """
    config = app_config.load_config()

    if config["ocr_setup_completed"] and not force_prompt:
        return config

    ocr_prompt_message = (
        "Would you like to enable OCR support for scanned documents and images?\n\n"
        "This lets Markdown Maker read text from scanned PDFs and photos of "
        "documents that have no selectable text. It's optional — you can enable "
        "this anytime later from the app."
    )
    if parent is not None:
        wants_ocr = messagebox.askyesno(
            title="Markdown Maker — OCR Support", message=ocr_prompt_message, parent=parent,
        )
    else:
        probe_root = tk.Tk()
        probe_root.withdraw()
        _bring_window_to_front(probe_root)
        wants_ocr = messagebox.askyesno(
            title="Markdown Maker — OCR Support", message=ocr_prompt_message, parent=probe_root,
        )
        probe_root.destroy()

    if not wants_ocr:
        return app_config.save_config(
            {"ocr_setup_completed": True, "ocr_enabled": False, "ocr_engine": None}
        )

    dialog = _OcrEngineChoiceDialog(parent=parent)
    engine_choice = dialog.show_and_wait()

    if engine_choice is None:
        return app_config.save_config(
            {"ocr_setup_completed": True, "ocr_enabled": False, "ocr_engine": None}
        )

    if engine_choice == "easyocr":
        success = _install_easyocr(parent=parent)
    else:
        success = _install_tesseract(parent=parent)

    return app_config.save_config(
        {
            "ocr_setup_completed": True,
            "ocr_enabled": success,
            "ocr_engine": engine_choice if success else None,
        }
    )
