"""Client for the signal-cli JSON-RPC daemon.

signal-cli runs as a long-lived daemon (`signal-cli daemon --tcp`) and
speaks newline-delimited JSON-RPC 2.0 over a TCP socket. This module keeps
one connection open, multiplexes request/response pairs by id, and turns
inbound notifications into events the app can act on.

Two receive paths matter here:

  * `envelope.dataMessage` with an audio attachment - a voice message for
    the child, which gets queued against a contact button.
  * `envelope.syncMessage.readMessages` - a read receipt echoed from another
    device on the same Signal account. This is what lets a contact's lamp go
    dark when a parent listens on their phone instead (requirement 14).

Requires signal-cli >= 0.14.2, which added `--voice-note` / `voiceNote`.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
RECONNECT_DELAY_MIN = 1.0
RECONNECT_DELAY_MAX = 30.0

# Signal sends voice notes as audio/*; iOS uses m4a, Android and Desktop opus.
AUDIO_CONTENT_PREFIX = "audio/"


class SignalError(RuntimeError):
    """A JSON-RPC error returned by signal-cli."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"signal-cli error {code}: {message}")
        self.code = code
        self.data = data


@dataclass(frozen=True)
class IncomingVoiceMessage:
    sender: str
    timestamp: int
    attachment: Path
    content_type: str


@dataclass(frozen=True)
class ReadReceipt:
    """Another device on this account read messages from `sender`."""

    sender: str
    up_to_timestamp: int


class SignalClient:
    def __init__(
        self,
        account: str,
        host: str = "127.0.0.1",
        port: int = 7583,
        attachment_dir: Path | None = None,
    ):
        self.account = account
        self.host = host
        self.port = port
        self.attachment_dir = Path(
            attachment_dir or Path.home() / ".local/share/signal-cli/attachments"
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._write_lock = asyncio.Lock()

        # Callbacks, set by the app before start().
        self.on_voice_message: Callable[[IncomingVoiceMessage], Awaitable[None]] | None = None
        self.on_read_receipt: Callable[[ReadReceipt], Awaitable[None]] | None = None
        self.on_connection_change: Callable[[bool], Awaitable[None]] | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="signal-client")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        """Connect, read forever, reconnect with backoff on failure."""
        delay = RECONNECT_DELAY_MIN
        while True:
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self.host, self.port
                )
                self._connected.set()
                await self._notify_connection(True)
                log.info("connected to signal-cli at %s:%s", self.host, self.port)
                delay = RECONNECT_DELAY_MIN
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError) as exc:
                log.warning("signal-cli connection failed: %s", exc)
            except Exception:
                log.exception("unexpected error in signal-cli reader")
            finally:
                was_connected = self._connected.is_set()
                self._connected.clear()
                await self._close()
                self._fail_pending(ConnectionError("signal-cli disconnected"))
                if was_connected:
                    await self._notify_connection(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("signal-cli closed the connection")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                log.warning("ignoring malformed line from signal-cli: %r", line[:200])
                continue
            try:
                await self._dispatch(payload)
            except Exception:
                log.exception("failed handling signal-cli payload")

    async def _close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def _notify_connection(self, up: bool) -> None:
        if self.on_connection_change:
            try:
                await self.on_connection_change(up)
            except Exception:
                log.exception("connection callback failed")

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # -- dispatch --------------------------------------------------------

    async def _dispatch(self, payload: dict) -> None:
        request_id = payload.get("id")
        if request_id is not None and ("result" in payload or "error" in payload):
            future = self._pending.pop(str(request_id), None)
            if future and not future.done():
                if "error" in payload:
                    err = payload["error"] or {}
                    future.set_exception(
                        SignalError(
                            int(err.get("code", -1)),
                            str(err.get("message", "unknown")),
                            err.get("data"),
                        )
                    )
                else:
                    future.set_result(payload["result"])
            return
        if payload.get("method") == "receive":
            await self._handle_envelope(
                (payload.get("params") or {}).get("envelope") or {}
            )

    async def _handle_envelope(self, envelope: dict) -> None:
        sender = (
            envelope.get("sourceNumber")
            or envelope.get("source")
            or envelope.get("sourceUuid")
            or ""
        )

        data_message = envelope.get("dataMessage")
        if data_message:
            await self._handle_data_message(sender, data_message)

        sync = envelope.get("syncMessage") or {}
        # A message the parent sent from their own phone also arrives here;
        # it is not for the child, but it does mean that conversation has
        # been attended to.
        for read in sync.get("readMessages") or []:
            other = read.get("sender") or read.get("senderNumber") or ""
            timestamp = read.get("timestamp")
            if other and timestamp and self.on_read_receipt:
                await self.on_read_receipt(
                    ReadReceipt(sender=other, up_to_timestamp=int(timestamp))
                )

        sent = sync.get("sentMessage")
        if sent and sent.get("destinationNumber") and self.on_read_receipt:
            # Parent replied from their phone: treat the conversation as seen.
            await self.on_read_receipt(
                ReadReceipt(
                    sender=str(sent["destinationNumber"]),
                    up_to_timestamp=int(sent.get("timestamp") or 0),
                )
            )

    async def _handle_data_message(self, sender: str, data_message: dict) -> None:
        timestamp = int(data_message.get("timestamp") or 0)
        for attachment in data_message.get("attachments") or []:
            content_type = str(attachment.get("contentType") or "")
            is_voice = bool(attachment.get("voiceNote"))
            if not (is_voice or content_type.startswith(AUDIO_CONTENT_PREFIX)):
                log.info("ignoring non-audio attachment (%s)", content_type)
                continue
            path = self._resolve_attachment(attachment)
            if path is None:
                log.warning("attachment from %s has no readable file", sender)
                continue
            if self.on_voice_message:
                await self.on_voice_message(
                    IncomingVoiceMessage(
                        sender=sender,
                        timestamp=timestamp,
                        attachment=path,
                        content_type=content_type,
                    )
                )

    def _resolve_attachment(self, attachment: dict) -> Path | None:
        """signal-cli writes attachments to disk and reports the id/filename."""
        for key in ("file", "filename"):
            value = attachment.get(key)
            if value and Path(value).is_absolute() and Path(value).exists():
                return Path(value)
        attachment_id = attachment.get("id")
        if attachment_id:
            candidate = self.attachment_dir / str(attachment_id)
            if candidate.exists():
                return candidate
        return None

    # -- requests --------------------------------------------------------

    async def _call(self, method: str, params: dict | None = None,
                    timeout: float = REQUEST_TIMEOUT) -> Any:
        if not self._connected.is_set():
            raise ConnectionError("not connected to signal-cli")
        request_id = str(next(self._ids))
        body = {"jsonrpc": "2.0", "method": method, "id": request_id}
        payload = dict(params or {})
        payload.setdefault("account", self.account)
        body["params"] = payload

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        line = (json.dumps(body) + "\n").encode()
        try:
            async with self._write_lock:
                writer = self._writer
                if writer is None:
                    raise ConnectionError("not connected to signal-cli")
                writer.write(line)
                await writer.drain()
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise SignalError(-1, f"{method} timed out after {timeout}s")
        finally:
            self._pending.pop(request_id, None)

    async def send_voice_note(self, recipient: str, audio_path: Path) -> int:
        """Send `audio_path` as a Signal voice note. Returns the send timestamp."""
        result = await self._call(
            "send",
            {
                "recipient": [recipient],
                "attachment": [_m4a_data_uri(audio_path)],
                "voiceNote": True,
            },
            timeout=120.0,  # uploading over a slow link can take a while
        )
        timestamp = int((result or {}).get("timestamp") or 0)
        log.info("sent voice note to %s (ts=%s)", recipient, timestamp)
        return timestamp

    async def send_receipt(self, recipient: str, timestamp: int,
                           receipt_type: str = "read") -> None:
        await self._call(
            "sendReceipt",
            {
                "recipient": recipient,
                "targetTimestamp": [int(timestamp)],
                "type": receipt_type,
            },
        )

    async def list_contacts(self) -> list[dict]:
        """Contacts known to the linked account, for nickname auto-fill."""
        result = await self._call("listContacts", {})
        entries = result if isinstance(result, list) else []
        contacts = []
        for entry in entries:
            number = entry.get("number") or ""
            name = (
                entry.get("name")
                or entry.get("profileName")
                or (entry.get("profile") or {}).get("givenName")
                or ""
            )
            if number:
                contacts.append({"number": number, "name": name.strip()})
        return contacts

    async def version(self) -> str:
        result = await self._call("version", {})
        if isinstance(result, dict):
            return str(result.get("version", "unknown"))
        return str(result)


def _m4a_data_uri(path: Path) -> str:
    """RFC 2397 data URI for `path`, tagged explicitly as audio/mp4.

    A plain filesystem path also works as an --attachment value, but then
    signal-cli has to guess the content-type from the file itself, and that
    guess is not reliable enough on a minimal headless Debian install: it
    can come back generic, and a generic content-type is exactly what makes
    a Signal client show a downloadable file instead of a voice-message
    bubble, even with voiceNote set. Spelling the type out here sidesteps
    the guess entirely - see signal-cli's own --attachment docs for the
    data: URI form.

    audio/mp4 (not audio/aac) because audio.py encodes to AAC inside an M4A
    container, not a bare AAC stream - audio/mp4 is the container's actual
    MIME type.

    Voice notes are capped at a minute and stay under ~1 MB (see audio.py),
    so reading the whole thing into memory to base64-encode it costs nothing
    worth avoiding - well under what one JSON-RPC round trip already costs.
    """
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:audio/mp4;filename={path.name};base64,{payload}"
