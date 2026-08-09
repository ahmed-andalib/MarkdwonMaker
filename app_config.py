"""
app_config.py — Small persisted user configuration (currently: OCR setup).

Stored at ~/.markdown_maker/config.json so the OCR install prompt is only
shown once per machine, and so converter.py can read the chosen engine at
conversion time without threading state through every layer of the app.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

CONFIG_DIR = Path.home() / ".markdown_maker"
CONFIG_PATH = CONFIG_DIR / "config.json"

_DEFAULTS: Dict[str, Any] = {
    "ocr_setup_completed": False,  # True once the user has answered the OCR prompt at all
    "ocr_enabled": False,
    "ocr_engine": None,  # "tesseract" | "easyocr" | None
    "tesseract_path": None,  # full path to tesseract.exe/tesseract, if not on PATH
    "theme": "light",  # "light" | "dark"
    "docling_setup_completed": False,  # True once the user has answered the Docling prompt at all
    "docling_enabled": False,
}


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return dict(_DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(_DEFAULTS)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)


def save_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    current = load_config()
    current.update(updates)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    return current
