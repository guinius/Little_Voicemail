"""Client for placing and receiving live Telegram calls.

This is deliberately a *separate* channel from Telegram messaging (voice
notes will go through Telegram's official Bot API - see
docs/telegram-migration.md). Bots cannot place or join calls at all; the
only way to do it is to drive a real, phone-verified personal Telegram
account over MTProto so it behaves like the official app does - Pyrogram
for the MTProto session, pytgcalls on top of it for the call/voice-chat
media itself.

That is a deliberate, accepted risk (see docs/telegram-migration.md,
"Calling: accepted risk"), not an oversight: Telegram's Bot API terms cover
official bots, not an automated personal account, and there is no
documented, supported way to avoid that. Concretely:

  * Messaging stays on the sanctioned Bot API path.
  * Calling is opt-in (config `telegram.calling.enabled`, defaults False)
    and isolated to this one module, so it can be disabled or ripped out
    without touching anything else.

Implementation note: pytgcalls' public documentation covers joining GROUP
voice chats in detail (GroupCallFactory/PyTgCalls.play etc.), which is not
what a 1:1 "call this contact" button needs. Its private-call surface
(request/accept/discard a direct call) exists - the library's own
description explicitly lists "make and receive private calls" - but at the
time this module was written its exact method names could not be confirmed
against the installed version from the documentation available. The
`_client` plumbing (Pyrogram session, connect/disconnect, event wiring) is
real; the four marked TODOs are where the actual private-call calls need
filling in and verifying against whatever pytgcalls version actually gets
pinned in requirements.txt, on real hardware, against a real account - not
something to guess confidently from outside that environment. Everything
above this module (PhoneApp's call state machine) is written against the
`TelegramCallClient` interface below, not against pytgcalls directly, so
that verification is contained to this one file.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

RECONNECT_DELAY_MIN = 1.0
RECONNECT_DELAY_MAX = 30.0


class TelegramCallError(RuntimeError):
    """Placing, answering or ending a call failed."""


class TelegramCallClient:
    """Owns the MTProto session used only for live calls.

    Mirrors SignalClient's shape (start()/stop(), a `connected` flag,
    callbacks set by the app before start()) deliberately, so PhoneApp
    wires this up the same way it already wires up messaging.
    """

    def __init__(
        self,
        api_id: str,
        api_hash: str,
        session_string: str,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_string = session_string

        self._pyrogram_client = None  # type: ignore[var-annotated]
        self._pytgcalls = None  # type: ignore[var-annotated]
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._current_peer: str | None = None

        # Callbacks, set by the app before start() - see PhoneApp._on_incoming_call
        # / _on_call_connected / _on_call_ended.
        self.on_incoming_call: Callable[[str], Awaitable[None]] | None = None
        self.on_call_connected: Callable[[], Awaitable[None]] | None = None
        self.on_call_ended: Callable[[str], Awaitable[None]] | None = None
        self.on_connection_change: Callable[[bool], Awaitable[None]] | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="telegram-call-client")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._disconnect()

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
        """Connect, stay connected, reconnect with backoff on failure -
        same shape as SignalClient._run(), for the same reason: a Pi on
        home WiFi loses its connection sometimes, and this should quietly
        pick back up rather than needing a service restart."""
        delay = RECONNECT_DELAY_MIN
        while True:
            try:
                await self._connect()
                self._connected.set()
                await self._notify_connection(True)
                delay = RECONNECT_DELAY_MIN
                # Pyrogram/pytgcalls run their own network loops once
                # started; idle here until stop() cancels this task.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram call client connection failed")
            finally:
                was_connected = self._connected.is_set()
                self._connected.clear()
                await self._disconnect()
                if was_connected:
                    await self._notify_connection(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _connect(self) -> None:
        # Imported lazily: pyrogram/pytgcalls (and their native
        # dependencies) are only required on a device with calling turned
        # on - importing them unconditionally would make every other box
        # depend on native libraries it never uses. See requirements.txt
        # (calling extras) and docs/telegram-migration.md.
        try:
            from pyrogram import Client  # type: ignore
            from pytgcalls import PyTgCalls  # type: ignore
        except ImportError as exc:
            raise TelegramCallError(
                "pyrogram/pytgcalls not installed; "
                "pip install -r requirements-calling.txt"
            ) from exc

        self._pyrogram_client = Client(
            name="little-voicemail-calls",
            api_id=self.api_id,
            api_hash=self.api_hash,
            session_string=self.session_string,
            in_memory=True,
        )
        await self._pyrogram_client.start()
        self._pytgcalls = PyTgCalls(self._pyrogram_client)

        # TODO(telegram-calling): wire pytgcalls' actual incoming-call
        # event to _handle_incoming(). The public docs available while
        # writing this only covered group voice chats
        # (`@pytgcalls_instance.on_update` / GroupCallFactory-style
        # handlers); confirm the private-call event name/signature against
        # the pinned pytgcalls version before relying on this.
        # self._pytgcalls.on_incoming_call(...)(self._handle_incoming)

        await self._pytgcalls.start()
        log.info("telegram call client connected")

    async def _disconnect(self) -> None:
        pytgcalls, self._pytgcalls = self._pytgcalls, None
        client, self._pyrogram_client = self._pyrogram_client, None
        if pytgcalls is not None:
            try:
                # TODO(telegram-calling): confirm pytgcalls' actual
                # shutdown call for the version pinned in requirements.txt.
                await pytgcalls.stop()
            except Exception:
                log.debug("pytgcalls stop() failed", exc_info=True)
        if client is not None:
            try:
                await client.stop()
            except Exception:
                log.debug("pyrogram client stop() failed", exc_info=True)

    async def _notify_connection(self, up: bool) -> None:
        if self.on_connection_change:
            try:
                await self.on_connection_change(up)
            except Exception:
                log.exception("connection callback failed")

    # -- incoming calls ------------------------------------------------

    async def _handle_incoming(self, caller_id: str) -> None:
        """Called by pytgcalls when a call arrives - see the TODO in
        _connect() for wiring this to the real event."""
        self._current_peer = caller_id
        if self.on_incoming_call:
            await self.on_incoming_call(caller_id)

    # -- call control --------------------------------------------------

    async def call(self, telegram_id: str) -> None:
        """Place an outgoing call to `telegram_id`. Returns once the call
        has been initiated (ringing), not once it is answered - answering
        arrives separately via on_call_connected."""
        if self._pytgcalls is None:
            raise TelegramCallError("not connected")
        self._current_peer = telegram_id
        # TODO(telegram-calling): the actual private-call request method
        # (pytgcalls' own description lists "make and receive private
        # calls" as a feature; confirm the exact call against the pinned
        # version - see the module docstring).
        raise TelegramCallError(
            "outgoing call not yet implemented - see TODO(telegram-calling) "
            "in telegram_call_client.py"
        )

    async def answer(self) -> None:
        """Accept the call currently ringing in (see on_incoming_call)."""
        if self._pytgcalls is None or self._current_peer is None:
            raise TelegramCallError("no incoming call to answer")
        # TODO(telegram-calling): accept-call method, see call().
        raise TelegramCallError(
            "answering a call not yet implemented - see TODO(telegram-calling) "
            "in telegram_call_client.py"
        )

    async def hang_up(self) -> None:
        """End whatever call (outgoing, incoming, or connected) is current.
        Deliberately tolerant of "nothing to hang up" - PhoneApp calls this
        unconditionally from _end_call() regardless of exactly which call
        state it is leaving."""
        if self._pytgcalls is None or self._current_peer is None:
            return
        peer, self._current_peer = self._current_peer, None
        try:
            # TODO(telegram-calling): discard/end-call method, see call().
            pass
        finally:
            log.info("call with %s ended", peer)

    async def decline(self) -> None:
        """Reject an incoming call without answering it."""
        await self.hang_up()
