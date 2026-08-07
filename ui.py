"""
ui.py — Tkinter/ttk layout, batch orchestration, and thread-safe UI updates.

Threading model
----------------
- A concurrent.futures.ThreadPoolExecutor runs converter.convert_file() for
  every queued file. Worker threads NEVER touch any Tkinter widget directly
  (Tk is not thread-safe).
- Each worker, upon finishing a file, pushes a ConversionResult onto a plain
  queue.Queue.
- The main thread polls that queue every 100ms via root.after(), and is the
  only code that ever mutates widgets. This is the standard, safe pattern
  for combining Tk with background threads.
"""

from __future__ import annotations

import datetime
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Dict, List, Optional, Set

from tkinterdnd2 import DND_FILES, TkinterDnD
import sv_ttk

import app_config
import bootstrap
import converter
import file_types

SUPPORTED_EXTENSIONS: Set[str] = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".csv", ".tsv", ".html", ".htm", ".txt", ".json", ".xml",
    ".epub",
} | file_types.IMAGE_EXTENSIONS

STATUS_QUEUED = "Queued"
STATUS_PROCESSING = "Processing"
STATUS_DONE = "Done"
STATUS_ERROR = "Error"
STATUS_CANCELLED = "Cancelled"

_MAX_WORKERS_CAP = 8
_TYPE_DETECTION_WORKERS = 4

# sv-ttk only themes ttk widgets automatically — the two plain tk widgets in
# this app (the drop-zone Listbox and the diagnostics Text pane) need their
# colors set manually to match, or they'd look like an unthemed leftover.
# These values mirror sv-ttk's own light/dark palettes.
_PLAIN_WIDGET_COLORS = {
    "light": {"bg": "#fafafa", "fg": "#1c1c1c", "field_bg": "#ffffff", "select_bg": "#0067c0"},
    "dark": {"bg": "#1c1c1c", "fg": "#fafafa", "field_bg": "#2b2b2b", "select_bg": "#0078d4"},
}
_TAG_COLORS = {
    "light": {"needs_ocr": "#B45309", "error_row": "#B91C1C"},
    "dark": {"needs_ocr": "#F5A623", "error_row": "#FF6B6B"},  # brighter for contrast on dark bg
}


class MarkdownMakerApp:
    def __init__(self, root: TkinterDnD.Tk):
        self.root = root
        self.root.title("Markdown Maker")
        self.root.geometry("1120x700")
        self.root.minsize(980, 600)

        # --- state -----------------------------------------------------
        self.queued_files: List[Path] = []
        self.row_id_by_path: Dict[str, str] = {}
        self.path_by_row_id: Dict[str, Path] = {}
        self.output_root: Optional[Path] = None
        self.input_root: Optional[Path] = None
        self.executor: Optional[ThreadPoolExecutor] = None
        self.futures: List[Future] = []
        self.event_queue: "queue.Queue" = queue.Queue()
        self.total_jobs = 0
        self.completed_jobs = 0
        self.error_count = 0
        self.is_running = False
        self.error_log_path: Optional[Path] = None
        self._error_log_lock = threading.Lock()

        # Type detection runs on its own small pool so it never competes
        # with, or blocks behind, the actual conversion batch.
        self._type_detect_executor = ThreadPoolExecutor(max_workers=_TYPE_DETECTION_WORKERS)

        self._build_layout()
        self._apply_theme(app_config.load_config().get("theme", "light"))
        self._refresh_ocr_status_label()
        self._poll_event_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _on_window_close(self) -> None:
        self._type_detect_executor.shutdown(wait=False, cancel_futures=True)
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use(style.theme_use())
        except tk.TclError:
            pass

        top_bar = ttk.Frame(self.root, padding=(12, 12, 12, 6))
        top_bar.pack(fill="x")

        file_actions_row = ttk.Frame(top_bar)
        file_actions_row.pack(fill="x")

        ttk.Button(file_actions_row, text="Add Files…", command=self._on_add_files).pack(side="left")
        ttk.Button(file_actions_row, text="Add Folder…", command=self._on_add_folder).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(file_actions_row, text="Clear Queue", command=self._on_clear_queue).pack(
            side="left", padx=(8, 0)
        )
        self.remove_button = ttk.Button(
            file_actions_row, text="Remove Selected", command=self._on_remove_selected, state="disabled"
        )
        self.remove_button.pack(side="left", padx=(8, 0))

        self.theme_toggle_button = ttk.Button(
            file_actions_row, text="🌙 Dark Mode", command=self._on_toggle_theme
        )
        self.theme_toggle_button.pack(side="right")

        settings_row = ttk.Frame(top_bar)
        settings_row.pack(fill="x", pady=(8, 0))

        self.output_label_var = tk.StringVar(value="Output folder: (choose one to start)")
        ttk.Button(settings_row, text="Choose Output Folder…", command=self._on_choose_output).pack(
            side="left"
        )

        self.ocr_status_var = tk.StringVar(value="OCR: checking…")
        ttk.Label(settings_row, textvariable=self.ocr_status_var).pack(side="right", padx=(0, 8))
        ttk.Button(settings_row, text="Configure OCR…", command=self._on_configure_ocr).pack(side="right")

        drop_frame = ttk.LabelFrame(self.root, text="Drop files or folders here", padding=8)
        drop_frame.pack(fill="both", expand=False, padx=12, pady=(0, 8))

        self.drop_target = tk.Listbox(drop_frame, height=3, activestyle="none")
        self.drop_target.insert(
            "end", "Drag & drop PDF / DOCX / XLSX / PPTX / CSV / HTML / TXT files or folders here"
        )
        self.drop_target.configure(state="disabled")
        self.drop_target.pack(fill="both", expand=True)
        self.drop_target.drop_target_register(DND_FILES)
        self.drop_target.dnd_bind("<<Drop>>", self._on_drop)

        ttk.Label(self.root, textvariable=self.output_label_var, padding=(12, 0)).pack(
            fill="x", anchor="w"
        )

        # --- queue table -------------------------------------------------
        table_frame = ttk.Frame(self.root, padding=(12, 6))
        table_frame.pack(fill="both", expand=True)

        columns = ("file", "type", "status", "engine", "detail")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("file", text="File")
        self.tree.heading("type", text="Type")
        self.tree.heading("status", text="Status")
        self.tree.heading("engine", text="Engine")
        self.tree.heading("detail", text="Detail")
        self.tree.column("file", width=300, anchor="w")
        self.tree.column("type", width=160, anchor="w")
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("engine", width=100, anchor="center")
        self.tree.column("detail", width=220, anchor="w")

        # Distinct tag styling so scanned/image files needing OCR stand out
        # in the list immediately, before conversion even starts.
        self.tree.tag_configure("needs_ocr", foreground="#B45309")
        self.tree.tag_configure("error_row", foreground="#B91C1C")

        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # Selection-driven enable/disable for the Remove button, a Delete-key
        # shortcut, and a right-click context menu — three ways to reach the
        # same removal action, since users reasonably expect all three.
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)
        self.tree.bind("<Delete>", lambda event: self._on_remove_selected())
        self.tree.bind("<Button-3>", self._on_tree_right_click)

        self._context_menu = tk.Menu(self.tree, tearoff=0)
        self._context_menu.add_command(label="Remove Selected", command=self._on_remove_selected)

        # --- progress + controls -----------------------------------------
        progress_frame = ttk.Frame(self.root, padding=(12, 6))
        progress_frame.pack(fill="x")

        self.status_var = tk.StringVar(value="Idle. Add files to begin.")
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w")

        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(4, 8))

        button_row = ttk.Frame(progress_frame)
        button_row.pack(fill="x")

        self.start_button = ttk.Button(button_row, text="Start Conversion", command=self._on_start)
        self.start_button.pack(side="left")

        self.cancel_button = ttk.Button(
            button_row, text="Cancel", command=self._on_cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(8, 0))

        worker_count = min(os.cpu_count() or 4, _MAX_WORKERS_CAP)
        ttk.Label(
            button_row, text=f"Worker threads: {worker_count} (capped to CPU cores)"
        ).pack(side="right")
        self.worker_count = worker_count

        # --- diagnostics pane ----------------------------------------------
        diag_frame = ttk.LabelFrame(self.root, text="Diagnostics", padding=8)
        diag_frame.pack(fill="both", expand=False, padx=12, pady=(0, 12))

        self.diag_text = tk.Text(diag_frame, height=7, wrap="word", state="disabled")
        diag_scroll = ttk.Scrollbar(diag_frame, orient="vertical", command=self.diag_text.yview)
        self.diag_text.configure(yscrollcommand=diag_scroll.set)
        self.diag_text.pack(side="left", fill="both", expand=True)
        diag_scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # File ingestion
    # ------------------------------------------------------------------
    def _on_add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Select files to convert")
        self._add_paths([Path(p) for p in paths])

    def _on_add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a folder to convert")
        if not folder:
            return
        self._add_paths(self._collect_folder_files(Path(folder)))

    def _on_drop(self, event) -> None:
        raw_paths = self.root.tk.splitlist(event.data)
        collected: List[Path] = []
        for raw in raw_paths:
            path = Path(raw)
            if path.is_dir():
                collected.extend(self._collect_folder_files(path))
            elif path.is_file():
                collected.append(path)
        self._add_paths(collected)

    def _detect_type_worker(self, path: Path) -> None:
        """Runs off the GUI thread; only ever pushes results onto the queue."""
        try:
            detected = file_types.detect_type(path)
        except Exception as exc:  # noqa: BLE001 - detection must never crash the app
            detected = file_types.DetectedType(
                label="Unknown", category="unknown", needs_ocr=False, detail=str(exc)
            )
        self.event_queue.put(("type_detected", path, detected))

    def _collect_folder_files(self, folder: Path) -> List[Path]:
        collected = []
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                collected.append(path)
        return collected

    def _add_paths(self, paths: List[Path]) -> None:
        added = 0
        for path in paths:
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            key = str(path.resolve())
            if key in self.row_id_by_path:
                continue
            self.queued_files.append(path)
            row_id = self.tree.insert(
                "", "end", values=(str(path), "Detecting…", STATUS_QUEUED, "", "")
            )
            self.row_id_by_path[key] = row_id
            self.path_by_row_id[row_id] = path
            self._type_detect_executor.submit(self._detect_type_worker, path)
            added += 1

        if added:
            self.status_var.set(f"{len(self.queued_files)} file(s) queued.")
        elif paths:
            messagebox.showinfo(
                "Markdown Maker",
                "No new supported files were added (unsupported type or already queued).",
            )

    def _on_clear_queue(self) -> None:
        if self.is_running:
            messagebox.showwarning("Markdown Maker", "Cannot clear the queue while running.")
            return
        self.queued_files.clear()
        self.row_id_by_path.clear()
        self.path_by_row_id.clear()
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.status_var.set("Idle. Add files to begin.")
        self.progress_bar["value"] = 0
        self.remove_button.configure(state="disabled")

    def _on_tree_selection_changed(self, _event=None) -> None:
        has_selection = bool(self.tree.selection())
        self.remove_button.configure(state="normal" if has_selection and not self.is_running else "disabled")

    def _on_tree_right_click(self, event) -> None:
        # Right-clicking a row that isn't already part of the selection
        # should select just that row, matching normal file-manager behavior,
        # rather than acting on a stale earlier selection.
        row_id = self.tree.identify_row(event.y)
        if row_id and row_id not in self.tree.selection():
            self.tree.selection_set(row_id)
        if self.tree.selection():
            self._context_menu.tk_popup(event.x_root, event.y_root)

    def _on_remove_selected(self) -> None:
        if self.is_running:
            messagebox.showwarning("Markdown Maker", "Cannot remove files while a conversion is running.")
            return
        selected_rows = self.tree.selection()
        if not selected_rows:
            return

        for row_id in selected_rows:
            path = self.path_by_row_id.pop(row_id, None)
            if path is not None:
                key = str(path.resolve())
                self.row_id_by_path.pop(key, None)
                if path in self.queued_files:
                    self.queued_files.remove(path)
            self.tree.delete(row_id)

        remaining = len(self.queued_files)
        self.status_var.set(f"{remaining} file(s) queued." if remaining else "Idle. Add files to begin.")
        self.remove_button.configure(state="disabled")

    def _apply_theme(self, theme: str) -> None:
        """Applies sv-ttk to all ttk widgets and manually colors the plain
        tk widgets (Listbox, Text) to match, since sv-ttk only auto-themes
        ttk widgets.

        sv-ttk applies its own baseline colorscheme to plain tk widgets too,
        but does so via a deferred/idle callback tied to its internal
        <<ThemeChanged>> handling — not synchronously inside set_theme().
        Setting our custom colors immediately after set_theme() would get
        silently overwritten the next time the event loop processes pending
        idle callbacks. Scheduling our coloring via after_idle() guarantees
        it runs after sv-ttk's own pass, so ours is the one that sticks.
        """
        theme = theme if theme in _PLAIN_WIDGET_COLORS else "light"
        sv_ttk.set_theme(theme)

        tag_colors = _TAG_COLORS[theme]
        self.tree.tag_configure("needs_ocr", foreground=tag_colors["needs_ocr"])
        self.tree.tag_configure("error_row", foreground=tag_colors["error_row"])

        self.theme_toggle_button.configure(
            text="🌙 Dark Mode" if theme == "light" else "☀️ Light Mode"
        )
        self._current_theme = theme

        def _apply_plain_widget_colors() -> None:
            colors = _PLAIN_WIDGET_COLORS[theme]
            self.drop_target.configure(
                bg=colors["field_bg"], fg=colors["fg"],
                selectbackground=colors["select_bg"], selectforeground="#ffffff",
                highlightthickness=0, relief="flat",
            )
            self.diag_text.configure(
                bg=colors["field_bg"], fg=colors["fg"],
                insertbackground=colors["fg"], selectbackground=colors["select_bg"],
                relief="flat",
            )

        self.root.after_idle(_apply_plain_widget_colors)

    def _on_toggle_theme(self) -> None:
        new_theme = "dark" if self._current_theme == "light" else "light"
        self._apply_theme(new_theme)
        app_config.save_config({"theme": new_theme})

    def _refresh_ocr_status_label(self) -> None:
        config = app_config.load_config()
        if config.get("ocr_enabled") and config.get("ocr_engine"):
            engine_label = {"tesseract": "Tesseract", "easyocr": "EasyOCR"}.get(
                config["ocr_engine"], config["ocr_engine"]
            )
            self.ocr_status_var.set(f"OCR: On ({engine_label})")
        else:
            self.ocr_status_var.set("OCR: Off")

    def _on_configure_ocr(self) -> None:
        if self.is_running:
            messagebox.showwarning("Markdown Maker", "Cannot change OCR settings while running.")
            return
        # Re-runs the same opt-in flow from bootstrap.py, forcing the prompt
        # to appear again regardless of any previous decision.
        bootstrap.ensure_ocr_setup(force_prompt=True, parent=self.root)
        self._refresh_ocr_status_label()

    def _on_choose_output(self) -> None:
        folder = filedialog.askdirectory(title="Choose an output folder for the Markdown files")
        if folder:
            self.output_root = Path(folder)
            self.output_label_var.set(f"Output folder: {self.output_root}")

    # ------------------------------------------------------------------
    # Batch execution
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        if self.is_running:
            return
        if not self.queued_files:
            messagebox.showinfo("Markdown Maker", "Add at least one file first.")
            return
        if self.output_root is None:
            messagebox.showinfo("Markdown Maker", "Choose an output folder first.")
            return

        self.input_root = self._compute_common_root(self.queued_files)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.error_log_path = self.output_root / "_conversion_errors.log"

        self.total_jobs = len(self.queued_files)
        self.completed_jobs = 0
        self.error_count = 0
        self.progress_bar.configure(maximum=self.total_jobs, value=0)
        self._set_diag_text("")

        self.is_running = True
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.remove_button.configure(state="disabled")
        self.status_var.set(f"Processing file 0 of {self.total_jobs}…")

        self.executor = ThreadPoolExecutor(max_workers=self.worker_count)
        self.futures = []

        for path in self.queued_files:
            key = str(path.resolve())
            row_id = self.row_id_by_path.get(key)
            if row_id:
                self.tree.set(row_id, "status", STATUS_PROCESSING)
            future = self.executor.submit(
                converter.convert_file, path, self.input_root, self.output_root
            )
            future.add_done_callback(self._make_done_callback(path))
            self.futures.append(future)

        # Executor stops accepting new work once shutdown() is called, but
        # we defer that to when the batch actually finishes so cancellation
        # can still call shutdown(cancel_futures=True) early.
        threading.Thread(target=self._shutdown_when_finished, daemon=True).start()

    def _compute_common_root(self, paths: List[Path]) -> Path:
        resolved = [p.resolve() for p in paths]
        parents = [p.parent for p in resolved]
        try:
            common = Path(os.path.commonpath([str(p) for p in parents]))
        except ValueError:
            # Paths span multiple drives (Windows) — fall back to each file's
            # own parent as its "root", which converter.py handles gracefully.
            common = resolved[0].parent
        return common

    def _make_done_callback(self, path: Path):
        def _callback(future: Future) -> None:
            if future.cancelled():
                self.event_queue.put(("cancelled", path, None))
                return
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - safety net around the future itself
                result = converter.ConversionResult(
                    source_path=path, success=False, error_message=str(exc)
                )
            self.event_queue.put(("result", path, result))

        return _callback

    def _shutdown_when_finished(self) -> None:
        for future in self.futures:
            future.result() if not future.cancelled() else None
        # wait() equivalent: iterate futures to block until all are done.
        if self.executor is not None:
            self.executor.shutdown(wait=True)
        self.event_queue.put(("batch_complete", None, None))

    def _on_cancel(self) -> None:
        if not self.is_running or self.executor is None:
            return
        cancelled_count = 0
        for future in self.futures:
            if future.cancel():
                cancelled_count += 1
        self.status_var.set(f"Cancelling… {cancelled_count} not-yet-started file(s) skipped.")
        self.cancel_button.configure(state="disabled")

    # ------------------------------------------------------------------
    # Thread-safe UI update loop
    # ------------------------------------------------------------------
    def _poll_event_queue(self) -> None:
        try:
            while True:
                kind, path, payload = self.event_queue.get_nowait()
                if kind == "result":
                    self._handle_result(path, payload)
                elif kind == "cancelled":
                    self._handle_cancelled(path)
                elif kind == "batch_complete":
                    self._handle_batch_complete()
                elif kind == "type_detected":
                    self._handle_type_detected(path, payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_event_queue)

    def _handle_type_detected(self, path: Path, detected: "file_types.DetectedType") -> None:
        key = str(path.resolve())
        row_id = self.row_id_by_path.get(key)
        if not row_id:
            return
        self.tree.set(row_id, "type", detected.label)
        if detected.needs_ocr:
            self.tree.item(row_id, tags=("needs_ocr",))

    def _handle_result(self, path: Path, result: "converter.ConversionResult") -> None:
        key = str(path.resolve())
        row_id = self.row_id_by_path.get(key)
        self.completed_jobs += 1

        if result.detected_type_label and row_id:
            self.tree.set(row_id, "type", result.detected_type_label)

        if result.success:
            if row_id:
                self.tree.set(row_id, "status", STATUS_DONE)
                self.tree.set(row_id, "engine", result.engine_used or "")
                detail = str(result.output_path) if result.output_path else ""
                if result.warning:
                    detail = f"⚠ {result.warning}"
                self.tree.set(row_id, "detail", detail)
            if result.warning:
                self._append_diag(
                    f"[{self._timestamp()}] WARNING for {path.name}: {result.warning}\n"
                )
        else:
            self.error_count += 1
            if row_id:
                self.tree.set(row_id, "status", STATUS_ERROR)
                self.tree.set(row_id, "engine", "/".join(result.engine_attempts))
                detail = result.error_message or "Unknown error"
                if result.warning:
                    detail = f"{result.warning} — {detail}"
                self.tree.set(row_id, "detail", detail)
                self.tree.item(row_id, tags=("error_row",))
            self._log_error(path, result)
            self._append_diag(
                f"[{self._timestamp()}] ERROR converting {path.name}: {result.error_message}\n"
            )

        self.progress_bar["value"] = self.completed_jobs
        self.status_var.set(
            f"Processing file {self.completed_jobs} of {self.total_jobs}… "
            f"({self.error_count} error(s) so far)"
        )

    def _handle_cancelled(self, path: Path) -> None:
        key = str(path.resolve())
        row_id = self.row_id_by_path.get(key)
        if row_id:
            self.tree.set(row_id, "status", STATUS_CANCELLED)

    def _handle_batch_complete(self) -> None:
        self.is_running = False
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._on_tree_selection_changed()  # re-enable Remove if something's still selected
        self.status_var.set(
            f"Finished: {self.completed_jobs} processed, {self.error_count} error(s). "
            f"Output in {self.output_root}"
        )
        if self.error_count:
            self._append_diag(
                f"[{self._timestamp()}] Batch complete with {self.error_count} error(s). "
                f"See {self.error_log_path} for details.\n"
            )
        else:
            self._append_diag(f"[{self._timestamp()}] Batch complete — no errors.\n")

    # ------------------------------------------------------------------
    # Diagnostics / logging helpers
    # ------------------------------------------------------------------
    def _log_error(self, path: Path, result: "converter.ConversionResult") -> None:
        if self.error_log_path is None:
            return
        with self._error_log_lock:
            with open(self.error_log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"[{self._timestamp()}] {path}\n")
                log_file.write(f"  Engines attempted: {', '.join(result.engine_attempts)}\n")
                log_file.write(f"  Error: {result.error_message}\n")
                if result.error_traceback:
                    log_file.write(f"  Traceback:\n{result.error_traceback}\n")
                log_file.write("\n")

    def _append_diag(self, text: str) -> None:
        self.diag_text.configure(state="normal")
        self.diag_text.insert("end", text)
        self.diag_text.see("end")
        self.diag_text.configure(state="disabled")

    def _set_diag_text(self, text: str) -> None:
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("end", text)
        self.diag_text.configure(state="disabled")

    @staticmethod
    def _timestamp() -> str:
        return datetime.datetime.now().strftime("%H:%M:%S")


def launch() -> None:
    root = TkinterDnD.Tk()
    MarkdownMakerApp(root)
    root.mainloop()