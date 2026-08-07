"""The device state machine.

Behaviour, in the order a child experiences it:

  * Press a contact button. Its lamp lights steady for 30 seconds, then the
    selection lapses back to standby.
  * If that contact has unheard messages, the first press plays them instead
    of selecting - you have to listen before you can reply.
  * Hold push-to-talk while a contact is selected. The PTT lamp lights and
    stays lit for as long as it records, up to a minute.
  * Let go. The clip is encoded and sent as a Signal voice note.
  * A message arriving plays the chosen ringtone and sets that contact's
    lamp flashing until it is heard - here or on a parent's own phone.
  * During quiet time none of that happens. Any press flashes all six
    lamps three times and is otherwise ignored; messages still arrive and
    queue up silently, appearing on the buttons once quiet time ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum
from pathlib import Path

from .audio import AudioEngine, AudioError
from .hardware import PTT, Action, ButtonEvent, Hardware, solid
from .messages import MessageQueue
from .quiet_hours import QuietHours
from .signal_client import IncomingVoiceMessage, ReadReceipt, SignalClient

log = logging.getLogger(__name__)


class State(Enum):
    IDLE = "idle"
    SELECTED = "selected"
    RECORDING = "recording"
    SENDING = "sending"
    PLAYING = "playing"


class PhoneApp:
    def __init__(
        self,
        config,
        hardware: Hardware,
        audio: AudioEngine,
        signal: SignalClient,
        queue: MessageQueue,
        status_path: Path | None = None,
    ):
        self.config = config
        self.hw = hardware
        self.audio = audio
        self.signal = signal
        self.queue = queue
        self.quiet = QuietHours(config)
        self.status_path = status_path

        self.state = State.IDLE
        self.selected_slot: int | None = None
        self._selection_expires: float = 0.0
        self._recording_slot: int | None = None
        self._busy = asyncio.Lock()
        self._was_quiet = self.quiet.is_quiet()
        self._tasks: list[asyncio.Task] = []
        # Sending runs detached so the button loop stays responsive, but it
        # is held onto so shutdown can wait for it instead of tearing the
        # database out from under a half-finished send.
        self._send_task: asyncio.Task | None = None
        # The last recording/encode/send failure, so a parent staring at the
        # System page can see *why* the lamp flashed instead of only that it
        # did - without going and finding journalctl. Cleared on the next
        # successful send, not on every attempt, so it survives long enough
        # to actually be read.
        self._last_error: str | None = None
        self._last_error_at: str = ""

        signal.on_voice_message = self._on_voice_message
        signal.on_read_receipt = self._on_read_receipt

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        self.hw.start()
        self.signal.start()
        self._refresh_leds()
        self._tasks = [
            asyncio.create_task(self._button_loop(), name="buttons"),
            asyncio.create_task(self._tick_loop(), name="tick"),
        ]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            raise
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        await self.wait_for_send(timeout=10.0)
        await self.signal.stop()
        await self.hw.stop()

    async def wait_for_send(self, timeout: float = 10.0) -> None:
        """Let an in-flight send finish (or give up) before tearing down."""
        task = self._send_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            log.warning("send did not finish within %.0fs; cancelling", timeout)
            task.cancel()
        except Exception:
            log.debug("send task ended with an error", exc_info=True)

    # -- periodic --------------------------------------------------------

    async def _tick_loop(self) -> None:
        """Expire selections and notice quiet time starting or ending."""
        last_status_write = 0.0
        while True:
            await asyncio.sleep(0.25)
            try:
                now = time.monotonic()
                if now - last_status_write >= 3.0:
                    last_status_write = now
                    self._write_status()
                if (
                    self.state is State.SELECTED
                    and self._selection_expires
                    and now >= self._selection_expires
                ):
                    log.info("selection of slot %s lapsed", self.selected_slot)
                    self._clear_selection()

                is_quiet = self.quiet.is_quiet()
                if is_quiet != self._was_quiet:
                    self._was_quiet = is_quiet
                    log.info("quiet time %s", "started" if is_quiet else "ended")
                    if is_quiet:
                        # Drop any half-finished interaction.
                        self._clear_selection()
                    # Ending quiet time reveals whatever queued up during it
                    # (requirement 12).
                    self._refresh_leds()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick loop error")

    # -- button handling -------------------------------------------------

    async def _button_loop(self) -> None:
        while True:
            event = await self.hw.buttons.events.get()
            try:
                await self._handle_button(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error handling %s", event)

    async def _handle_button(self, event: ButtonEvent) -> None:
        if self.quiet.is_quiet():
            # Only answer the initial press, so holding a button does not
            # queue up a stack of flash sequences.
            if event.action is Action.PRESS:
                await self.hw.leds.flash_all(times=3)
                self._refresh_leds()
            return

        if event.is_ptt:
            await self._handle_ptt(event)
        elif event.action is Action.PRESS:
            await self._handle_contact_press(event.slot)

    async def _handle_contact_press(self, slot: int) -> None:
        if self.state in (State.RECORDING, State.SENDING, State.PLAYING):
            return
        contact = self.config.contact(slot)
        if contact is None:
            log.debug("slot %s is unassigned; ignoring", slot)
            return

        if self.queue.pending_for_slot(slot):
            await self._play_pending(slot, contact)
            return

        if self.selected_slot == slot:
            self._clear_selection()  # press again to deselect
        else:
            self._select(slot)

    def _select(self, slot: int) -> None:
        timeout = float(
            self.config.get("behaviour", "selection_timeout_seconds", default=30)
        )
        self.selected_slot = slot
        self.state = State.SELECTED
        self._selection_expires = time.monotonic() + timeout
        log.info("selected slot %s for %.0fs", slot, timeout)
        self._refresh_leds()

    def _clear_selection(self) -> None:
        self.selected_slot = None
        self._selection_expires = 0.0
        if self.state in (State.SELECTED, State.IDLE):
            self.state = State.IDLE
        self._refresh_leds()

    # -- listening -------------------------------------------------------

    async def _play_pending(self, slot: int, contact: dict) -> None:
        """Play every unheard message from this contact, oldest first."""
        async with self._busy:
            pending = self.queue.pending_for_slot(slot)
            if not pending:
                return
            self.state = State.PLAYING
            self.hw.leds.apply_contact_states(selected=slot, pending={})
            log.info("playing %d message(s) from slot %s", len(pending), slot)

            async def retire(message):
                self.queue.mark_played(message.id)
                # Tell the sender it was heard, and let the parent's other
                # devices mark the thread read too.
                try:
                    await self.signal.send_receipt(message.sender, message.signal_ts)
                except Exception:
                    log.warning("could not send read receipt", exc_info=True)

            try:
                await self.audio.play_sequence(pending, on_played=retire)
            finally:
                # Listening no longer auto-selects the contact for reply -
                # the child presses the button again if they want to talk
                # back, the same as any other press. Auto-selecting made it
                # too easy to hold PTT and start replying to whoever was
                # last played without meaning to.
                self.state = State.IDLE
                self._clear_selection()

    # -- recording and sending -------------------------------------------

    async def _handle_ptt(self, event: ButtonEvent) -> None:
        if event.action is Action.PRESS:
            await self._start_recording()
        elif event.action is Action.RELEASE:
            await self._finish_recording()

    async def _start_recording(self) -> None:
        if self.state is not State.SELECTED or self.selected_slot is None:
            log.debug("push-to-talk with nothing selected; ignoring")
            return
        contact = self.config.contact(self.selected_slot)
        if contact is None:
            return
        try:
            await self.audio.start_recording()
        except AudioError as exc:
            log.exception("could not start recording")
            self._record_error(f"could not start recording: {exc}")
            return
        self.state = State.RECORDING
        self._recording_slot = self.selected_slot
        # Freeze the selection while recording - the 30s timer must not
        # snatch the contact away mid-sentence.
        self._selection_expires = 0.0
        self.hw.leds.set(PTT, solid())
        log.info("recording for slot %s", self._recording_slot)

    async def _finish_recording(self) -> None:
        if self.state is not State.RECORDING:
            return
        self.hw.leds.off(PTT)
        slot = self._recording_slot
        self._recording_slot = None
        try:
            recording = await self.audio.stop_recording()
        except AudioError as exc:
            log.exception("could not stop recording")
            self._record_error(f"could not stop recording: {exc}")
            self._clear_selection()
            return

        if recording.aborted or slot is None:
            self.state = State.SELECTED if slot else State.IDLE
            if slot:
                self._select(slot)
            return

        contact = self.config.contact(slot)
        if contact is None:
            self._clear_selection()
            return

        self.state = State.SENDING
        self._send_task = asyncio.create_task(
            self._send(slot, contact, recording), name=f"send-slot-{slot}"
        )

    async def _send(self, slot: int, contact: dict, recording) -> None:
        """Encode and deliver, keeping the lamp lit until it is on its way."""
        async with self._busy:
            self.hw.leds.set(slot, solid())
            try:
                ogg = await self.audio.encode_voice_note(recording.path)
                await self.signal.send_voice_note(contact["number"], ogg)
                log.info(
                    "sent %.1fs voice note to %s (slot %s)",
                    recording.duration, contact["name"] or contact["number"], slot,
                )
                self._last_error = None  # a good send outweighs a stale complaint
            except Exception as exc:
                log.exception("failed to send voice note to slot %s", slot)
                self._record_error(
                    f"send to slot {slot} failed: {type(exc).__name__}: {exc}"
                )
                await self._indicate_failure(slot)
            finally:
                try:
                    Path(recording.path).with_suffix(".m4a").unlink(missing_ok=True)
                except OSError:
                    pass
                self.state = State.IDLE
                self._clear_selection()

    async def _indicate_failure(self, slot: int) -> None:
        """Blink the contact's own lamp quickly so the child knows to retry."""
        from .hardware import blink

        self.hw.leds.set(slot, blink(period=0.2, duty=0.5))
        await asyncio.sleep(2.0)
        self.hw.leds.off(slot)

    def _record_error(self, message: str) -> None:
        """Remember the reason for the last failure, for the System page."""
        self._last_error = message
        self._last_error_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # -- inbound ---------------------------------------------------------

    async def _on_voice_message(self, message: IncomingVoiceMessage) -> None:
        slot = self.config.slot_for_number(message.sender)
        if slot is None:
            log.info("voice message from unknown number %s; ignoring", message.sender)
            return
        row_id = self.queue.add(
            slot=slot,
            sender=message.sender,
            signal_ts=message.timestamp,
            attachment=str(message.attachment),
        )
        if row_id is None:
            return  # already queued

        if self.quiet.is_quiet():
            # Silent during quiet time; the lamp lights when it ends.
            log.info("queued message for slot %s silently (quiet time)", slot)
            return

        log.info("voice message for slot %s", slot)
        self._refresh_leds()
        if self.state not in (State.RECORDING, State.PLAYING):
            await self.audio.play_ringtone()

    async def _on_read_receipt(self, receipt: ReadReceipt) -> None:
        cleared = self.queue.mark_read_elsewhere(
            receipt.sender, receipt.up_to_timestamp
        )
        if cleared:
            log.info(
                "%d message(s) from %s read on another device", cleared, receipt.sender
            )
            self._refresh_leds()

    # -- LEDs ------------------------------------------------------------

    def _refresh_leds(self) -> None:
        self.hw.leds.apply_contact_states(
            selected=self.selected_slot,
            pending=self.queue.pending_counts(),
            muted=self.quiet.is_quiet(),
        )

    # -- status for the web UI -------------------------------------------

    def _write_status(self) -> None:
        """Publish state for the web UI, which runs as a separate process."""
        if self.status_path is None:
            return
        try:
            payload = json.dumps(self.status())
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.status_path)
        except OSError:
            log.debug("could not write status file", exc_info=True)

    def status(self) -> dict:
        active = self.quiet.active_window()
        return {
            "state": self.state.value,
            "selected_slot": self.selected_slot,
            "pending": self.queue.pending_counts(),
            "total_pending": self.queue.total_pending(),
            "quiet": active is not None,
            "quiet_window": active.label if active else None,
            "quiet_until": (
                self.quiet.quiet_until().isoformat() if active else None
            ),
            "signal_connected": self.signal.connected,
            "hardware_live": self.hw.live,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at or None,
        }
