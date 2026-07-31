"""Filesystem locations.

Installed on a Pi everything lives under /opt/little-voicemail with state in
/var/lib/little-voicemail. Run from a git checkout it keeps to the checkout,
so a developer can try the web UI on a laptop without touching /var.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

SYSTEM_DATA_DIR = Path("/var/lib/little-voicemail")
SYSTEM_CONFIG_DIR = Path("/etc/little-voicemail")


def _installed() -> bool:
    """True when running from the system install location."""
    return str(PROJECT_ROOT).startswith("/opt/little-voicemail")


def default_data_dir() -> Path:
    if env := os.environ.get("LV_DATA_DIR"):
        return Path(env)
    if _installed():
        return SYSTEM_DATA_DIR
    return PROJECT_ROOT / "var"


def default_config_path() -> Path:
    if env := os.environ.get("LV_CONFIG"):
        return Path(env)
    if _installed():
        return SYSTEM_CONFIG_DIR / "config.json"
    return PROJECT_ROOT / "var" / "config.json"


def default_sounds_dir() -> Path:
    if env := os.environ.get("LV_SOUNDS_DIR"):
        return Path(env)
    return PROJECT_ROOT / "sounds"


def signal_config_dir() -> Path:
    """Where signal-cli keeps its account and session state."""
    if env := os.environ.get("LV_SIGNAL_CONFIG"):
        return Path(env)
    if _installed():
        return SYSTEM_DATA_DIR / "signal-cli"
    return Path.home() / ".local/share/signal-cli"


def signal_attachment_dir() -> Path:
    return signal_config_dir() / "attachments"


def certs_dir() -> Path:
    return default_data_dir() / "certs"
