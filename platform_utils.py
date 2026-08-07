"""
platform_utils.py — Host platform detection and Tesseract install-command
construction.

Used only by the optional OCR setup flow in bootstrap.py. EasyOCR needs
none of this since it's a pure pip package; Tesseract's actual OCR engine
is a native binary that pip cannot install, so we need to know the exact
host (Windows/macOS/which Linux distro) to hand back the right command.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from typing import List, Optional, Tuple

_MACOS_CODENAMES = {
    15: "Sequoia", 14: "Sonoma", 13: "Ventura", 12: "Monterey", 11: "Big Sur",
}

# Windows 11 reports platform.release() == "10" on many Python/CPython builds;
# the reliable signal is the build number, which crossed 22000 for Windows 11.
_WINDOWS_11_BUILD_THRESHOLD = 22000


@dataclass
class HostPlatform:
    system: str  # "Windows", "Darwin", "Linux", or whatever platform.system() returns
    friendly_name: str  # e.g. "Windows 11", "macOS 15.1 (Sequoia)", "Ubuntu 24.04 LTS"
    distro_id: Optional[str] = None  # Linux only: "ubuntu", "fedora", "arch", ...
    package_manager: Optional[str] = None  # "winget", "choco", "brew", "apt-get", "dnf", "pacman", "zypper"
    needs_elevation: bool = False  # whether the install command needs root/admin


def detect_host_platform() -> HostPlatform:
    system = platform.system()

    if system == "Windows":
        return _detect_windows()
    if system == "Darwin":
        return _detect_macos()
    if system == "Linux":
        return _detect_linux()

    return HostPlatform(system=system or "Unknown", friendly_name=system or "Unknown host")


def _detect_windows() -> HostPlatform:
    release = platform.release()
    friendly = f"Windows {release}"
    try:
        build = int(platform.version().split(".")[-1])
        if build >= _WINDOWS_11_BUILD_THRESHOLD:
            friendly = "Windows 11"
        elif release:
            friendly = f"Windows {release}"
    except (ValueError, IndexError):
        pass

    package_manager = None
    if shutil.which("winget"):
        package_manager = "winget"
    elif shutil.which("choco"):
        package_manager = "choco"

    # winget generally works per-user without elevation; choco commonly needs
    # an elevated (Administrator) shell.
    needs_elevation = package_manager == "choco"
    return HostPlatform(
        system="Windows",
        friendly_name=friendly,
        package_manager=package_manager,
        needs_elevation=needs_elevation,
    )


def _detect_macos() -> HostPlatform:
    mac_ver = platform.mac_ver()[0]
    codename = ""
    try:
        major = int(mac_ver.split(".")[0]) if mac_ver else 0
        codename = _MACOS_CODENAMES.get(major, "")
    except ValueError:
        pass
    friendly = f"macOS {mac_ver}" + (f" ({codename})" if codename else "") if mac_ver else "macOS"

    package_manager = "brew" if shutil.which("brew") else None
    return HostPlatform(
        system="Darwin",
        friendly_name=friendly,
        package_manager=package_manager,
        needs_elevation=False,  # Homebrew installs as the current user, not root
    )


def _read_os_release() -> Tuple[Optional[str], Optional[str]]:
    try:
        info = platform.freedesktop_os_release()  # Python 3.10+
        return info.get("ID"), info.get("PRETTY_NAME")
    except (AttributeError, OSError):
        pass
    try:
        data = {}
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    key, _, value = line.strip().partition("=")
                    data[key] = value.strip().strip('"')
        return data.get("ID"), data.get("PRETTY_NAME")
    except OSError:
        return None, None


def _detect_linux_package_manager(distro_id: Optional[str]) -> Optional[str]:
    family_map = {
        "ubuntu": ["apt-get", "apt"],
        "debian": ["apt-get", "apt"],
        "linuxmint": ["apt-get", "apt"],
        "pop": ["apt-get", "apt"],
        "fedora": ["dnf", "yum"],
        "rhel": ["dnf", "yum"],
        "centos": ["dnf", "yum"],
        "rocky": ["dnf", "yum"],
        "almalinux": ["dnf", "yum"],
        "arch": ["pacman"],
        "manjaro": ["pacman"],
        "endeavouros": ["pacman"],
        "opensuse": ["zypper"],
        "opensuse-leap": ["zypper"],
        "opensuse-tumbleweed": ["zypper"],
        "sles": ["zypper"],
    }
    candidates = family_map.get(distro_id or "", ["apt-get", "apt", "dnf", "yum", "pacman", "zypper"])
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    return None


def _detect_linux() -> HostPlatform:
    distro_id, distro_name = _read_os_release()
    friendly = distro_name or "Linux"
    package_manager = _detect_linux_package_manager(distro_id)
    return HostPlatform(
        system="Linux",
        friendly_name=friendly,
        distro_id=distro_id,
        package_manager=package_manager,
        needs_elevation=package_manager is not None,  # apt/dnf/pacman/zypper all need root
    )


def build_tesseract_install_command(host: HostPlatform) -> Optional[List[str]]:
    """
    Returns a subprocess-ready argv list to install Tesseract on this host,
    or None if no automated install path is available (caller should fall
    back to manual instructions).

    If host.needs_elevation is True and `pkexec` is available (Linux desktop
    polkit agent), the caller is expected to prefix the command with pkexec;
    this function returns the unprivileged command either way so the caller
    can decide how to elevate.
    """
    if host.system == "Windows":
        if host.package_manager == "winget":
            return [
                "winget", "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
                "--silent", "--accept-source-agreements", "--accept-package-agreements",
            ]
        if host.package_manager == "choco":
            return ["choco", "install", "tesseract", "-y"]
        return None

    if host.system == "Darwin":
        if host.package_manager == "brew":
            return ["brew", "install", "tesseract"]
        return None

    if host.system == "Linux":
        pm = host.package_manager
        if pm in ("apt-get", "apt"):
            return [pm, "install", "-y", "tesseract-ocr"]
        if pm in ("dnf", "yum"):
            return [pm, "install", "-y", "tesseract"]
        if pm == "pacman":
            return ["pacman", "-S", "--noconfirm", "tesseract"]
        if pm == "zypper":
            return ["zypper", "--non-interactive", "install", "tesseract-ocr"]
        return None

    return None


def manual_install_instructions(host: HostPlatform) -> str:
    if host.system == "Windows":
        return (
            "Download and run the Tesseract installer for Windows from:\n"
            "https://github.com/UB-Mannheim/tesseract/wiki\n\n"
            "After installing, make sure the install folder "
            "(usually C:\\Program Files\\Tesseract-OCR) is added to your PATH."
        )
    if host.system == "Darwin":
        return (
            "Install Homebrew first (https://brew.sh), then run:\n"
            "  brew install tesseract"
        )
    if host.system == "Linux":
        return (
            "Install Tesseract using your distribution's package manager, e.g.:\n"
            "  Debian/Ubuntu:   sudo apt-get install tesseract-ocr\n"
            "  Fedora/RHEL:     sudo dnf install tesseract\n"
            "  Arch/Manjaro:    sudo pacman -S tesseract\n"
            "  openSUSE:        sudo zypper install tesseract-ocr"
        )
    return "Please install Tesseract OCR manually for your platform, then restart Markdown Maker."


def tesseract_binary_available() -> bool:
    return find_tesseract_executable() is not None


def find_tesseract_executable() -> Optional[str]:
    """
    Locates the Tesseract binary, returning its full path, or None if it
    can't be found anywhere.

    Checks PATH first, then falls back to well-known install locations.
    This matters because some installers — notably the UB-Mannheim Windows
    installer when run silently via winget — install Tesseract without
    adding it to PATH (the "Add to PATH" step is an installer task that
    silent/unattended runs frequently skip). Relying on PATH alone means a
    perfectly successful install can look like a failure.
    """
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path

    candidates: List[str] = []
    system = platform.system()

    if system == "Windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app_data = os.environ.get("LocalAppData", "")
        candidates += [
            os.path.join(program_files, "Tesseract-OCR", "tesseract.exe"),
            os.path.join(program_files_x86, "Tesseract-OCR", "tesseract.exe"),
        ]
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "Programs", "Tesseract-OCR", "tesseract.exe"))

    elif system == "Darwin":
        candidates += [
            "/opt/homebrew/bin/tesseract",  # Apple Silicon Homebrew
            "/usr/local/bin/tesseract",  # Intel Homebrew
        ]

    elif system == "Linux":
        candidates += [
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
        ]

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    return None
