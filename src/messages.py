"""Inbound voice-message queue.

Backed by SQLite so the queue survives a reboot or a power cut - a message
that arrived at bedtime is still waiting on the button in the morning.

A message leaves the queue in one of three ways:
  * the child listens to it on the device        -> reason "played"
  * a parent reads it on their phone or desktop, and Signal syncs a read
    receipt back to this linked device           -> reason "read_elsewhere"
  * a parent clears the queue from the web UI    -> reason "reset"
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slot            INTEGER NOT NULL,
    sender          TEXT    NOT NULL,
    signal_ts       INTEGER NOT NULL,
    attachment      TEXT    NOT NULL,
    received_at     REAL    NOT NULL,
    cleared_at      REAL,
    cleared_reason  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe
    ON messages (sender, signal_ts);
CREATE INDEX IF NOT EXISTS idx_messages_pending
    ON messages (slot, cleared_at);
"""


@dataclass(frozen=True)
class Message:
    id: int
    slot: int
    sender: str
    signal_ts: int
    attachment: str
    received_at: float


class MessageQueue:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        # WAL keeps the web UI's reads from blocking the phone app's writes.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ----------------------------------------------------------

    def add(self, slot: int, sender: str, signal_ts: int, attachment: str) -> int | None:
        """Queue an inbound voice message.

        Returns the new row id, or None if this exact message was already
        queued - Signal can redeliver on reconnect and we must not make the
        child listen to the same thing twice.
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO messages (slot, sender, signal_ts, attachment,"
                    " received_at) VALUES (?, ?, ?, ?, ?)",
                    (slot, sender, int(signal_ts), str(attachment), time.time()),
                )
            except sqlite3.IntegrityError:
                log.debug("duplicate message from %s ts=%s ignored", sender, signal_ts)
                return None
            return int(cur.lastrowid)

    def mark_played(self, message_id: int) -> None:
        self._clear("id = ?", (message_id,), reason="played")

    def clear_slot(self, slot: int, reason: str = "reset") -> int:
        return self._clear("slot = ?", (slot,), reason=reason)

    def clear_all(self, reason: str = "reset") -> int:
        return self._clear("1 = 1", (), reason=reason)

    def mark_read_elsewhere(self, sender: str, up_to_ts: int) -> int:
        """Clear messages a parent has already read on another device.

        Signal's read receipts identify the message by its original send
        timestamp, and reading one message on a phone implicitly reads
        everything older in that conversation - hence `<=`.
        """
        return self._clear(
            "sender = ? AND signal_ts <= ?",
            (sender, int(up_to_ts)),
            reason="read_elsewhere",
        )

    def _clear(self, where: str, params: tuple, reason: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE messages SET cleared_at = ?, cleared_reason = ?"
                f" WHERE cleared_at IS NULL AND ({where})",
                (time.time(), reason, *params),
            )
            return cur.rowcount or 0

    def prune(self, keep_days: int = 30) -> int:
        """Drop long-cleared rows so the SD card doesn't fill up."""
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE cleared_at IS NOT NULL AND cleared_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

    # -- reads -----------------------------------------------------------

    def pending_for_slot(self, slot: int) -> list[Message]:
        """Oldest-first, so playback runs in the order they were spoken."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE slot = ? AND cleared_at IS NULL"
                " ORDER BY signal_ts ASC",
                (slot,),
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def pending_counts(self) -> dict[int, int]:
        """Slot -> number of unheard messages. Drives the flashing LEDs."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT slot, COUNT(*) AS n FROM messages"
                " WHERE cleared_at IS NULL GROUP BY slot"
            ).fetchall()
        return {int(r["slot"]): int(r["n"]) for r in rows}

    def total_pending(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE cleared_at IS NULL"
            ).fetchone()
        return int(row["n"])

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM messages ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=int(row["id"]),
        slot=int(row["slot"]),
        sender=str(row["sender"]),
        signal_ts=int(row["signal_ts"]),
        attachment=str(row["attachment"]),
        received_at=float(row["received_at"]),
    )
