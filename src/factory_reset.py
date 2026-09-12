"""Factory reset (GitHub issue #19).

Wipes every setting a parent has made - contacts, quiet times, the ringtone
choice, the parent password, the linked Signal account and the saved home
WiFi network - and reboots straight back into first-run setup, the same
state the box was in the day it was flashed.

Reachable two ways, both ending up here:

  * The physical combo - hold contact buttons 1 and 2 together for 10
    seconds - detected by PhoneApp so it works even if the web UI (or the
    parent's password) is the thing that's unusable.
  * A "Factory reset" button under System -> Advanced in the web UI, for a
    parent who would rather not touch the box.

Best-effort throughout: the box is rebooting into first-run setup
regardless of how much of this succeeds, so a file that could not be
removed is logged and skipped rather than treated as a reason to stop
partway through and leave the device in a half-reset state.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .paths import default_config_path, default_data_dir, signal_config_dir

log = logging.getLogger(__name__)

NETCTL = "/usr/local/lib/little-voicemail/lv-netctl"


def wipe(config_path: Path | None = None, data_dir: Path | None = None) -> None:
    """Delete every stored setting, message, and linked account."""
    config_path = Path(config_path or default_config_path())
    data_dir = Path(data_dir or default_data_dir())

    log.warning("factory reset: wiping %s and %s", config_path, data_dir)

    _remove(config_path)
    _remove(config_path.with_suffix(".json.broken"))

    if data_dir.exists():
        for child in data_dir.iterdir():
            if child.name == "certs":
                # The HTTPS certificate isn't a parent-visible "setting" -
                # it regenerates itself automatically (see web/server.py)
                # and a fresh device would otherwise mint a brand new CA on
                # its very next boot, which would break nothing but is pure
                # churn for no benefit.
                continue
            _remove_tree(child)

    _remove_tree(signal_config_dir())
    forget_wifi()


def forget_wifi() -> None:
    """Delete the saved home-WiFi profile so the setup hotspot comes back."""
    try:
        result = subprocess.run(
            ["sudo", "-n", NETCTL, "forget"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning(
                "could not forget the WiFi network: %s",
                (result.stdout + result.stderr).strip()[-300:],
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("could not forget the WiFi network: %s", exc)


def reboot() -> tuple[bool, str]:
    """Reboot the device. Same sudoers rule updater.py's own reboot uses."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "/sbin/reboot"], capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove %s", path, exc_info=True)


def _remove_tree(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        log.warning("could not remove %s", path, exc_info=True)
