"""Configuration store for Little Voicemail.

The config file is the single source of truth for everything a parent can
change from the web UI: the six contact slots, the ringtone, and the three
quiet-time windows. It is written atomically so a power cut mid-save can
never leave a truncated file on the SD card.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

NUM_CONTACTS = 6

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    # Parent web UI login. Populated by install.sh / first-run setup.
    "web": {
        "password_hash": "",
        "port": 8443,
        "hostname": "littlevoicemail",
    },
    "signal": {
        # The Signal account this device is linked to, in E.164 form.
        # Written by the Signal page in the web UI, never by hand.
        "account": "",
        "jsonrpc_host": "127.0.0.1",
        "jsonrpc_port": 7583,
    },
    "network": {
        # If the box boots with no network it raises its own access point so
        # the WiFi can be set from a browser. Without this there is no way
        # into a screenless box that cannot reach the LAN.
        "setup_ap": {
            "enabled": True,
            "ssid": "Little Voicemail setup",
            "password": "voicemail",
            "grace_seconds": 60,
        },
    },
    # Six slots, always present, indexed 1-6. An unassigned slot has an
    # empty number and is inert: its button does nothing.
    "contacts": [
        {"slot": i, "name": "", "number": "", "enabled": False}
        for i in range(1, NUM_CONTACTS + 1)
    ],
    "audio": {
        "ringtone": "chime.wav",
        "input_device": "plughw:CARD=seeed2micvoicec,DEV=0",
        "output_device": "plughw:CARD=seeed2micvoicec,DEV=0",
        "playback_gap_seconds": 1.0,
        "max_record_seconds": 60,
        "min_record_seconds": 0.7,
        "ringtone_volume": 0.8,
    },
    "behaviour": {
        "selection_timeout_seconds": 30,
        # Require the child to listen to a pending message before they are
        # allowed to reply to that contact (requirement 6).
        "require_listen_before_reply": True,
    },
    # Three independent windows: school, nap, bedtime. Times are local
    # "HH:MM" and a window may wrap past midnight.
    "quiet_times": [
        {"id": "school", "label": "School", "enabled": False,
         "start": "09:00", "end": "15:15", "days": [0, 1, 2, 3, 4]},
        {"id": "nap", "label": "Nap", "enabled": False,
         "start": "13:00", "end": "14:30", "days": [0, 1, 2, 3, 4, 5, 6]},
        {"id": "bedtime", "label": "Bedtime", "enabled": False,
         "start": "19:00", "end": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]},
    ],
    "updates": {
        "repo": "guinius/Little_Voicemail",
        "branch": "master",
        "check_on_load": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` onto a copy of `base`, recursing into dicts.

    Lists are replaced wholesale, not merged - a parent editing contacts
    should be able to clear a slot without the old value bleeding through.
    """
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Thread-safe accessor around the on-disk JSON config."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self.load()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
                self.save()
                return self._data
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    stored = json.load(fh)
            except (json.JSONDecodeError, OSError):
                # A corrupt config must not brick the device; fall back to
                # defaults and keep the bad file around for diagnosis.
                broken = self.path.with_suffix(".json.broken")
                try:
                    self.path.replace(broken)
                except OSError:
                    pass
                stored = {}
            self._data = _deep_merge(DEFAULT_CONFIG, stored)
            self._normalise()
            return self._data

    def _normalise(self) -> None:
        """Guarantee six contact slots numbered 1-6, in order."""
        by_slot = {
            c.get("slot"): c
            for c in self._data.get("contacts", [])
            if isinstance(c, dict)
        }
        self._data["contacts"] = [
            {
                "slot": i,
                "name": str(by_slot.get(i, {}).get("name", "") or ""),
                "number": str(by_slot.get(i, {}).get("number", "") or ""),
                "enabled": bool(by_slot.get(i, {}).get("enabled", False)),
            }
            for i in range(1, NUM_CONTACTS + 1)
        ]

    def save(self) -> None:
        """Atomically replace the config file."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".config-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # -- accessors -------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        with self._lock:
            return self._data

    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            return node

    def set(self, value: Any, *keys: str) -> None:
        if not keys:
            raise ValueError("set() needs at least one key")
        with self._lock:
            node = self._data
            for key in keys[:-1]:
                node = node.setdefault(key, {})
            node[keys[-1]] = value
            self.save()

    def contacts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(c) for c in self._data["contacts"]]

    def contact(self, slot: int) -> dict[str, Any] | None:
        """Return the contact in `slot` (1-6), or None if unassigned."""
        if not 1 <= slot <= NUM_CONTACTS:
            return None
        with self._lock:
            entry = self._data["contacts"][slot - 1]
            if not entry["enabled"] or not entry["number"]:
                return None
            return dict(entry)

    def slot_for_number(self, number: str) -> int | None:
        """Map an inbound Signal number back to a button slot."""
        target = _normalise_number(number)
        if not target:
            return None
        with self._lock:
            for entry in self._data["contacts"]:
                if entry["enabled"] and _normalise_number(entry["number"]) == target:
                    return int(entry["slot"])
        return None

    def set_contact(
        self, slot: int, name: str, number: str, enabled: bool = True
    ) -> None:
        if not 1 <= slot <= NUM_CONTACTS:
            raise ValueError(f"slot must be 1-{NUM_CONTACTS}, got {slot}")
        with self._lock:
            self._data["contacts"][slot - 1] = {
                "slot": slot,
                "name": name.strip(),
                "number": number.strip(),
                "enabled": bool(enabled and number.strip()),
            }
            self.save()

    def clear_contact(self, slot: int) -> None:
        self.set_contact(slot, "", "", enabled=False)


def _normalise_number(number: str) -> str:
    """Reduce a phone number to digits plus a leading '+' for comparison."""
    if not number:
        return ""
    cleaned = "".join(ch for ch in number if ch.isdigit() or ch == "+")
    return cleaned.lstrip("+")
