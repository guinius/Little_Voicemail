"""The device state machine.

Behaviour, in the order a child experiences it:

  * On startup, once buttons/lights/audio are ready, chime.wav plays once
    to say the box is awake - unless quiet time is running, in which case
    it stays silent like everything else during quiet time.
  * Press a contact button. Its lamp lights steady for 30 seconds, then the
    selection lapses back to standby.
  * If that contact has unheard messages, the first press plays them instead
    of selecting - you have to listen before you can reply.
  * Hold push-to-talk while a contact is selected. The PTT lamp lights and
    stays lit for as long as it records, up to a minute.
  * Let go. The clip is encoded and sent as a Signal voice note in the
    background - the child can pick another contact and record again right
    away rather than waiting for it to actually go out, up to MAX_OUTBOX
    messages in flight at once (see _queue_send).
  * A message arriving plays the chosen ringtone and sets that contact's
    lamp flashing until it is heard - here or on a parent's own phone.
  * Hold a contact button for CALL_HOLD_SECONDS (rather than letting go
    for a voice note) and the device places a live call to them instead -
    see docs/telegram-migration.md for why this is Telegram-only, and only
    when a parent has opted calling in. That contact's lamp blinks slowly
    while it rings out; press push-to-talk to hang up before it is
    answered, same as ending a connected call.
  * An incoming call rings (a looping ringtone) and flashes that contact's
    lamp quickly until either that same button is pressed (answers - the
    lamp goes solid) or it goes unanswered for the configured ring
    timeout (treated as missed, same as it going unheard would be).
    Every other button is inert for as long as a call is ringing,
    dialling out, or connected - the same "something important is
    happening, do not let a stray press derail it" rule already applied
    to recording/sending/listening - so a wrong contact pressed by
    accident during a call does nothing rather than dropping it or
    redirecting it; only push-to-talk ends a call, deliberately the one
    unambiguous control for that. Calls do not ring in during quiet time;
    they are declined the same way a press is ignored then.
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
from .config import NUM_CONTACTS
from .hardware import PTT, Action, ButtonEvent, Hardware, blink, solid
from .messages import MessageQueue
from .quiet_hours import QuietHours
from .signal_client import IncomingVoiceMessage, ReadReceipt, SignalClient
from .telegram_call_client import TelegramCallClient

log = logging.getLogger(__name__)

# Safety net for button test mode: if a parent forgets to turn it off, the
# device would otherwise sit there ignoring every real button press
# indefinitely. Ten minutes is long enough to work through every button
# with some to spare, short enough that a forgotten test mode does not
# strand the device for the rest of the day.
TEST_MODE_MAX_SECONDS = 600

# Factory reset (GitHub issue #19): hold contact buttons 1 and 2 together
# for this long and the device wipes every setting and reboots into
# first-run setup. Deliberately two specific buttons rather than any pair -
# a child leaning on the pad, or a stuck button, should not be able to
# trigger it by accident the way "any two at once" could.
FACTORY_RESET_SLOTS = (1, 2)
FACTORY_RESET_HOLD_SECONDS = 10.0
# The LED render loop only ticks at 25 Hz (RENDER_INTERVAL in leds.py), so
# this is as fast a flash as it can actually show, not a literal 50 Hz -
# still unmistakably different from any of the app's normal lamp patterns.
FACTORY_RESET_FLASH_SECONDS = 3.0

# How many recordings can be mid-encode/mid-send at once before a new
# push-to-talk has to wait its turn. Below this, finishing a recording
# hands it off to the background and the child can go straight back to
# selecting another contact - see _queue_send(). It is a cap on background
# work, not a size limit chosen for its own sake: five short voicemails
# encoding/uploading at once is already more than this hardware would want
# to be doing simultaneously.
MAX_OUTBOX = 5

# How long a contact button has to be held, continuously, before it stops
# meaning "select this contact" and starts meaning "call this contact" -
# long enough that reaching for push-to-talk to record a normal voice note
# (a quick press, then let go) never has a chance of being misread as the
# start of a call.
CALL_HOLD_SECONDS = 3.0
# LED patterns for the three call states, distinct from every other pattern
# this app uses: dialling out blinks slower than an incoming ring, so the
# two are told apart at a glance; both are faster than the ~1s pending-
# message blink so a call reads as more urgent, and neither is the sending
# blink's period either.
CALLING_BLINK = blink(period=0.8, duty=0.5)
RINGING_BLINK = blink(period=0.4, duty=0.5)


class State(Enum):
    IDLE = "idle"
    SELECTED = "selected"
    RECORDING = "recording"
    SENDING = "sending"
    PLAYING = "playing"
    CALLING = "calling"    # dialling out; not yet answered
    RINGING = "ringing"    # an inbound call, not yet answered
    IN_CALL = "in_call"    # connected, either direction


class PhoneApp:
    def __init__(
        self,
        config,
        hardware: Hardware,
        audio: AudioEngine,
        signal: SignalClient,
        queue: MessageQueue,
        calls: TelegramCallClient | None = None,
        status_path: Path | None = None,
        test_mode_flag_path: Path | None = None,
    ):
        self.config = config
        self.hw = hardware
        self.audio = audio
        self.signal = signal
        self.queue = queue
        # None until a parent opts calling in (see main.py) - every call
        # code path below checks for that before doing anything, so the
        # feature is fully inert rather than half-wired when it's off.
        self.calls = calls
        self.quiet = QuietHours(config)
        self.status_path = status_path
        # Button test mode is toggled by the web UI creating/deleting this
        # file. It can't just flip a value in config.json instead - Config
        # is loaded once at startup and this (the phone service) never
        # rereads it, so an already-running process would never notice a
        # change made by the web process. The tick loop polls for the
        # file's existence instead, which needs no reload machinery at all.
        self._test_mode_flag_path = test_mode_flag_path

        self.state = State.IDLE
        self.selected_slot: int | None = None
        self._selection_expires: float = 0.0
        self._recording_slot: int | None = None
        # The slot a call (dialling out, ringing in, or connected) is with;
        # meaningful only while self.state is CALLING/RINGING/IN_CALL. See
        # _begin_outgoing_call / _on_incoming_call / _end_call.
        self._call_slot: int | None = None
        self._call_started: float = 0.0
        self._ring_task: asyncio.Task | None = None
        self._busy = asyncio.Lock()
        self._was_quiet = self.quiet.is_quiet()
        self._tasks: list[asyncio.Task] = []
        # Sending runs detached so the button loop stays responsive, and
        # several can be in flight together (see MAX_OUTBOX/_queue_send) -
        # each task is held onto, keyed to the slot it is sending for, so
        # shutdown can wait for all of them instead of tearing the database
        # out from under a half-finished send, and so LED rendering can
        # tell which contacts still have a send outstanding.
        self._send_tasks: dict[asyncio.Task, int] = {}
        # The last recording/encode/send failure, so a parent staring at the
        # System page can see *why* the lamp flashed instead of only that it
        # did - without going and finding journalctl. Cleared on the next
        # successful send, not on every attempt, so it survives long enough
        # to actually be read.
        self._last_error: str | None = None
        self._last_error_at: str = ""
        # Button test mode: press any button, it lights only its own lamp
        # and gets recorded here instead of doing anything else - a pure
        # hardware loopback check for wiring new buttons. See _poll_test_mode.
        self._test_mode = False
        self._test_mode_since = 0.0
        self._test_events: dict[int, dict] = {}
        # Guards the factory-reset combo (see _check_factory_reset_combo) so
        # it fires exactly once even though the flash-then-wipe sequence
        # takes several seconds, during which the buttons are typically
        # still held.
        self._factory_reset_triggered = False

        signal.on_voice_message = self._on_voice_message
        signal.on_read_receipt = self._on_read_receipt
        if self.calls is not None:
            self.calls.on_incoming_call = self._on_incoming_call
            self.calls.on_call_connected = self._on_call_connected
            self.calls.on_call_ended = self._on_call_ended_remotely

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        # A leftover flag from an unclean previous exit (crash, power cut)
        # must not make this boot come up already in test mode with no
        # parent watching - startup, not just graceful shutdown, is what
        # reliably covers that case.
        if self._test_mode_flag_path is not None:
            try:
                self._test_mode_flag_path.unlink(missing_ok=True)
            except OSError:
                pass
        self.hw.start()
        self.signal.start()
        if self.calls is not None:
            self.calls.start()
        self._refresh_leds()
        await self._play_boot_chime()
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
        if self._ring_task is not None:
            self._ring_task.cancel()
            self._ring_task = None
        await self.wait_for_send(timeout=10.0)
        await self.signal.stop()
        if self.calls is not None:
            await self.calls.stop()
        await self.hw.stop()

    async def _play_boot_chime(self) -> None:
        """A short "I'm awake" chime once startup finishes, so a parent
        knows the box actually came back up after a power cycle without
        having to check the web UI. Skipped during quiet time - the whole
        point of quiet time is that the box stays silent, and a boot chime
        is exactly the kind of noise a power cut at 2am would otherwise
        cause. Always chime.wav specifically, not whatever ringtone is
        configured for messages - this is a distinct "I've booted" signal,
        not a stand-in for one, so it stays fixed even if a parent changes
        the message ringtone.
        """
        if self.quiet.is_quiet():
            return
        chime = self.audio.sounds_dir / "chime.wav"
        if not chime.exists():
            log.warning("chime.wav missing from %s; skipping boot chime",
                       self.audio.sounds_dir)
            return
        await self.audio.play(chime)

    async def wait_for_send(self, timeout: float = 10.0) -> None:
        """Let any in-flight sends finish (or give up) before tearing down."""
        tasks = [t for t in self._send_tasks if not t.done()]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout
            )
        except asyncio.TimeoutError:
            log.warning(
                "%d send(s) did not finish within %.0fs; cancelling",
                len(tasks), timeout,
            )
            for task in tasks:
                task.cancel()

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
                # Checked unconditionally, ahead of everything else here and
                # regardless of quiet time or button test mode - it is the
                # one escape hatch meant to work no matter what state the
                # box has gotten itself into.
                self._check_factory_reset_combo(now)
                self._poll_test_mode(now)
                self._check_call_hold(now)
                await self._check_call_timeouts(now)
                if (
                    self.state is State.SELECTED
                    and self._selection_expires
                    and now >= self._selection_expires
                ):
                    log.info("selection of slot %s lapsed", self.selected_slot)
                    self._clear_selection()

                await self._check_quiet_time_transition()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick loop error")

    async def _check_quiet_time_transition(self) -> None:
        """React to quiet time starting or ending - split out from
        _tick_loop so a test can call it directly instead of racing the
        real 0.25s poll (same reason _check_factory_reset_combo etc. are
        their own methods)."""
        is_quiet = self.quiet.is_quiet()
        if is_quiet == self._was_quiet:
            return
        self._was_quiet = is_quiet
        log.info("quiet time %s", "started" if is_quiet else "ended")
        if is_quiet:
            # Drop any half-finished interaction - a call in progress
            # included, same as a selection is.
            if self.state in (State.CALLING, State.RINGING, State.IN_CALL):
                await self._end_call(reason="quiet time started")
            self._clear_selection()
        # Ending quiet time reveals whatever queued up during it
        # (requirement 12) - but not over test mode's own LED display,
        # which owns the lamps until it exits.
        if not self._test_mode:
            self._refresh_leds()

    # -- factory reset -----------------------------------------------------

    def _check_factory_reset_combo(self, now: float) -> None:
        """Buttons 1 and 2 held together for FACTORY_RESET_HOLD_SECONDS."""
        if self._factory_reset_triggered:
            return
        starts = [self.hw.buttons.held_since(slot) for slot in FACTORY_RESET_SLOTS]
        if any(start is None for start in starts):
            return
        # Timed from whichever of the two was pressed *last* - both have to
        # be held together for the full duration, not just overlap briefly.
        if now - max(starts) < FACTORY_RESET_HOLD_SECONDS:
            return
        self._factory_reset_triggered = True
        log.warning(
            "factory reset triggered: buttons %s held for %.0fs",
            FACTORY_RESET_SLOTS, FACTORY_RESET_HOLD_SECONDS,
        )
        self._tasks.append(
            asyncio.create_task(self._run_factory_reset(), name="factory-reset")
        )

    async def _run_factory_reset(self) -> None:
        from . import factory_reset

        try:
            cycle = 0.2  # on+off per blink; see FACTORY_RESET_FLASH_SECONDS
            times = max(1, round(FACTORY_RESET_FLASH_SECONDS / cycle))
            await self.hw.leds.flash_all(times=times, on=cycle / 2, off=cycle / 2)
        except Exception:
            log.exception("factory reset flash failed; resetting anyway")
        factory_reset.wipe()
        ok, detail = factory_reset.reboot()
        if not ok:
            log.error("factory reset: reboot command failed: %s", detail)

    # -- button test mode --------------------------------------------------

    def _poll_test_mode(self, now: float) -> None:
        """Enter/exit test mode by noticing the flag file's existence.

        Called every tick (0.25s) rather than on some longer status-write
        cadence, so a parent toggling the web UI's button sees it react
        close to instantly rather than after a multi-second lag.
        """
        if self._test_mode_flag_path is None:
            return
        active = self._test_mode_flag_path.exists()
        if active and not self._test_mode:
            self._enter_test_mode(now)
        elif self._test_mode and not active:
            self._exit_test_mode()
        elif self._test_mode and now - self._test_mode_since > TEST_MODE_MAX_SECONDS:
            log.warning("button test mode left on too long; turning it off")
            self._exit_test_mode()
            try:
                self._test_mode_flag_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _enter_test_mode(self, now: float) -> None:
        log.info("button test mode started")
        self._test_mode = True
        self._test_mode_since = now
        self._test_events = {}
        self._clear_selection()
        self.hw.leds.all_off()
        self._write_status()

    def _exit_test_mode(self) -> None:
        log.info("button test mode ended")
        self._test_mode = False
        self._test_events = {}
        self._refresh_leds()
        self._write_status()

    def _handle_test_button(self, event: ButtonEvent) -> None:
        """Light exactly the pressed button's own lamp and record what
        happened for the web UI - a pure hardware loopback check, none of
        the normal selecting/recording/sending behaviour."""
        slot = event.slot
        if event.action is Action.PRESS:
            self.hw.leds.set(slot, solid())
        elif event.action is Action.RELEASE:
            self.hw.leds.off(slot)
        self._test_events[slot] = {
            "action": event.action.value,
            "at": time.strftime("%H:%M:%S"),
            "duration": round(event.duration, 2) if event.duration else None,
        }
        # Test mode is exactly the situation where a parent is staring at
        # the page waiting for feedback - the normal 3s status-write
        # throttle would make every press feel unresponsive.
        self._write_status()

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
        if self._test_mode:
            self._handle_test_button(event)
            return

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
        if self.state is State.RINGING:
            if slot == self._call_slot:
                await self._answer_call()
            else:
                # A different button pressed while one is ringing: ignored,
                # not treated as declining or redirecting the call - see
                # the module docstring and docs/telegram-migration.md for
                # why. Only the ringing contact's own button answers.
                log.debug(
                    "slot %s pressed while slot %s is ringing; ignoring",
                    slot, self._call_slot,
                )
            return
        if self.state in (
            State.RECORDING, State.SENDING, State.PLAYING,
            State.CALLING, State.IN_CALL,
        ):
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
            self.hw.leds.apply_contact_states(
                selected=slot, pending={},
                sending=frozenset(self._send_tasks.values()),
            )
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
            if self.state in (State.CALLING, State.RINGING, State.IN_CALL):
                # The one unambiguous "stop" for every call state - dial-
                # ling out, ringing in unanswered, or connected - rather
                # than also overloading the contact button's own press for
                # some of those (see the module docstring).
                await self._end_call()
                return
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

        self._queue_send(slot, contact, recording)

    def _queue_send(self, slot: int, contact: dict, recording) -> None:
        """Hand a finished recording to the outbox for background encoding
        and sending.

        Below MAX_OUTBOX concurrent sends, the child can go straight back
        to selecting another contact and recording again - state drops to
        idle immediately rather than sitting on SENDING (which blocks any
        new selection/recording, see _handle_contact_press/_start_recording)
        for however long this send takes. Once MAX_OUTBOX are already in
        flight, this falls back to the previous behaviour: stay busy until
        one of them finishes and frees a slot (_on_send_done), so the
        outbox itself can never grow past MAX_OUTBOX.
        """
        task = asyncio.create_task(
            self._send(slot, contact, recording), name=f"send-slot-{slot}"
        )
        self._send_tasks[task] = slot
        task.add_done_callback(self._on_send_done)

        if len(self._send_tasks) >= MAX_OUTBOX:
            self.state = State.SENDING
            self._refresh_leds()  # picks up this slot's "sending" blink
        else:
            self.state = State.IDLE
            self._clear_selection()

    def _on_send_done(self, task: asyncio.Task) -> None:
        """Reconcile shared state once one background send finishes.

        Runs for every send, not just ones that filled the outbox - each
        completion always needs the just-finished slot dropped from the
        "sending" set so its lamp stops blinking, but must never touch
        self.state/selection except in the one case those were actually
        left pointing at this send (the outbox was full when it started).
        Otherwise the app has long since moved on - possibly mid a new
        recording - and clobbering state here would cut that short.
        """
        self._send_tasks.pop(task, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("send task ended unexpectedly", exc_info=task.exception())
        if self.state is State.SENDING and len(self._send_tasks) < MAX_OUTBOX:
            self.state = State.IDLE
            self._clear_selection()  # also refreshes LEDs
        else:
            self._refresh_leds()

    async def _send(self, slot: int, contact: dict, recording) -> None:
        """Encode and deliver one recording in the background.

        Several of these can run at once (see _queue_send) - each only
        touches its own contact's lamp state indirectly (through the
        "sending" set _refresh_leds reads) plus its own exception handling
        and file cleanup. None of it touches self.state or the current
        selection directly, since those are shared across every in-flight
        send; _on_send_done reconciles those centrally once this one ends.
        """
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

    async def _indicate_failure(self, slot: int) -> None:
        """Blink the contact's own lamp quickly so the child knows to retry."""
        self.hw.leds.set(slot, blink(period=0.2, duty=0.5))
        await asyncio.sleep(2.0)
        self.hw.leds.off(slot)

    def _record_error(self, message: str) -> None:
        """Remember the reason for the last failure, for the System page."""
        self._last_error = message
        self._last_error_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # -- calling -----------------------------------------------------------
    #
    # Outgoing: hold a contact button for CALL_HOLD_SECONDS while it is
    # selected (_check_call_hold, polled from the tick loop the same way
    # _check_factory_reset_combo already polls held_since() for the reset
    # combo - a call is a continuous hold, not a single button event, so it
    # can't be driven off _handle_button the way a press/release can).
    # Incoming: _on_incoming_call, wired to calls.on_incoming_call in
    # __init__. Either way, only push-to-talk ends it (_handle_ptt) and any
    # other contact button is ignored for as long as it lasts
    # (_handle_contact_press) - see the module docstring for why.

    def _check_call_hold(self, now: float) -> None:
        if self.calls is None:
            return
        if self.state is not State.SELECTED or self.selected_slot is None:
            return
        if self.quiet.is_quiet():
            # Belt and braces: unreachable today (nothing gets selected
            # during quiet time to begin with - see _handle_button), kept
            # explicit so a future change to that can't accidentally let a
            # call ring out or in during quiet time.
            return
        started = self.hw.buttons.held_since(self.selected_slot)
        if started is None or now - started < CALL_HOLD_SECONDS:
            return
        contact = self.config.contact(self.selected_slot)
        if contact is None or not contact.get("telegram_id"):
            return  # nothing to call - stays selected for a voice note instead
        self._begin_outgoing_call(self.selected_slot, contact)

    def _begin_outgoing_call(self, slot: int, contact: dict) -> None:
        # State flips before anything async happens (see _run_outgoing_call)
        # so this can only ever fire once per hold: the next tick's
        # _check_call_hold sees state is no longer SELECTED and does
        # nothing, the same way _queue_send flips state before its send
        # task starts.
        self.state = State.CALLING
        self._call_slot = slot
        self._call_started = time.monotonic()
        self._selection_expires = 0.0
        self.hw.leds.contacts_off()
        self.hw.leds.set(slot, CALLING_BLINK)
        log.info("calling slot %s", slot)
        self._tasks.append(
            asyncio.create_task(self._run_outgoing_call(slot, contact), name=f"call-slot-{slot}")
        )

    async def _run_outgoing_call(self, slot: int, contact: dict) -> None:
        try:
            assert self.calls is not None
            await self.calls.call(contact["telegram_id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("failed to call slot %s", slot)
            self._record_error(f"call to slot {slot} failed: {type(exc).__name__}: {exc}")
            if self.state is State.CALLING and self._call_slot == slot:
                await self._end_call(reason="failed to connect")

    async def _check_call_timeouts(self, now: float) -> None:
        if self.calls is None:
            return
        if self.state is State.RINGING:
            timeout = float(
                self.config.get("telegram", "calling", "ring_timeout_seconds", default=30)
            )
            if now - self._call_started >= timeout:
                log.info("call from slot %s went unanswered", self._call_slot)
                await self._end_call(reason="missed")
        elif self.state is State.CALLING:
            timeout = float(
                self.config.get("telegram", "calling", "dial_timeout_seconds", default=45)
            )
            if now - self._call_started >= timeout:
                log.info("call to slot %s went unanswered", self._call_slot)
                await self._end_call(reason="no answer")

    async def _on_incoming_call(self, caller_telegram_id: str) -> None:
        """calls.on_incoming_call - a call is arriving right now."""
        slot = self.config.slot_for_telegram_id(caller_telegram_id)
        if slot is None:
            log.info("call from unknown contact %s; declining", caller_telegram_id)
            await self._safe_decline()
            return
        if self.quiet.is_quiet():
            # Same rule as a message arriving during quiet time: nothing
            # rings, nothing lights up. Unlike a message, a call has no
            # queue to sit in - there's no equivalent of it "appearing on
            # the button once quiet time ends" for a parent to fall back
            # on, so this is a real trade-off, not just quiet time applied
            # mechanically; flagged in docs/telegram-migration.md as worth
            # a parent confirming rather than assumed obvious.
            log.info("declining call to slot %s during quiet time", slot)
            await self._safe_decline()
            return
        if self.state is not State.IDLE:
            log.info("declining call to slot %s; device busy (%s)", slot, self.state.value)
            await self._safe_decline()
            return

        self.state = State.RINGING
        self._call_slot = slot
        self._call_started = time.monotonic()
        self.hw.leds.contacts_off()
        self.hw.leds.set(slot, RINGING_BLINK)
        log.info("incoming call for slot %s", slot)
        self._ring_task = asyncio.create_task(self._ring_loop(), name="ring")

    async def _safe_decline(self) -> None:
        try:
            assert self.calls is not None
            await self.calls.decline()
        except Exception:
            log.exception("failed to decline incoming call")

    async def _ring_loop(self) -> None:
        """Loop the ringtone until the call is answered, declined, or times
        out - play_ringtone() only plays once, unlike a message's chime."""
        try:
            while True:
                await self.audio.play_ringtone()
                await asyncio.sleep(0.4)  # a small gap, same idea as playback_gap_seconds
        except asyncio.CancelledError:
            raise

    async def _on_call_connected(self) -> None:
        """calls.on_call_connected - the far end picked up (whichever side
        placed the call)."""
        if self.state not in (State.CALLING, State.RINGING):
            return
        if self._ring_task is not None:
            self._ring_task.cancel()
            self._ring_task = None
        await self.audio.stop_playback()
        self.state = State.IN_CALL
        if self._call_slot is not None:
            self.hw.leds.set(self._call_slot, solid())
        log.info("call with slot %s connected", self._call_slot)

    async def _answer_call(self) -> None:
        """The child pressed the ringing contact's own button."""
        if self._ring_task is not None:
            self._ring_task.cancel()
            self._ring_task = None
        await self.audio.stop_playback()
        try:
            assert self.calls is not None
            await self.calls.answer()
        except Exception as exc:
            log.exception("failed to answer call")
            self._record_error(f"could not answer call: {type(exc).__name__}: {exc}")
            await self._end_call(reason="answer failed")
            return
        self.state = State.IN_CALL
        if self._call_slot is not None:
            self.hw.leds.set(self._call_slot, solid())
        log.info("answered call from slot %s", self._call_slot)

    async def _end_call(self, reason: str = "hung up") -> None:
        """Ends a call in any of the three call states - dialling out,
        ringing in, or connected - via push-to-talk, a ring/dial timeout,
        quiet time starting, or the far end hanging up first
        (_on_call_ended_remotely). Always safe to call: if there is
        nothing to hang up, calls.hang_up() is a no-op (see its docstring).
        """
        slot = self._call_slot
        if self._ring_task is not None:
            self._ring_task.cancel()
            self._ring_task = None
        await self.audio.stop_playback()
        if self.calls is not None:
            try:
                await self.calls.hang_up()
            except Exception:
                log.exception("hang_up failed")
        log.info("call with slot %s ended (%s)", slot, reason)
        self._call_slot = None
        self.state = State.IDLE
        self._clear_selection()

    async def _on_call_ended_remotely(self, reason: str) -> None:
        """calls.on_call_ended - the far end hung up, declined, or the
        connection dropped, rather than the child ending it here."""
        if self.state in (State.CALLING, State.RINGING, State.IN_CALL):
            await self._end_call(reason=reason or "ended by other side")

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
        if self.state in (State.CALLING, State.RINGING, State.IN_CALL):
            # A call state owns the lamps directly (_begin_outgoing_call /
            # _on_incoming_call / _answer_call / _on_call_connected) the
            # same way button test mode does - apply_contact_states()
            # would stomp that pattern on the next unrelated refresh
            # (an incoming message, say) if it ran here too.
            return
        self.hw.leds.apply_contact_states(
            selected=self.selected_slot,
            pending=self.queue.pending_counts(),
            muted=self.quiet.is_quiet(),
            sending=frozenset(self._send_tasks.values()),
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
            "call_slot": self._call_slot,
            "pending": self.queue.pending_counts(),
            "total_pending": self.queue.total_pending(),
            "quiet": active is not None,
            "quiet_window": active.label if active else None,
            "quiet_until": (
                self.quiet.quiet_until().isoformat() if active else None
            ),
            "signal_connected": self.signal.connected,
            "calls_connected": self.calls.connected if self.calls else None,
            "hardware_live": self.hw.live,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at or None,
            "test_mode": self._test_mode,
            "test_events": self._test_events_for_status(),
        }

    def _test_events_for_status(self) -> list[dict]:
        """Every button, PTT first, each with its last recorded test-mode
        action - not just the ones pressed so far, so the web UI can show
        a full checklist rather than only a growing log."""
        if not self._test_mode:
            return []
        slots = (PTT, *range(1, NUM_CONTACTS + 1))
        empty = {"action": None, "at": None, "duration": None}
        return [
            {
                "slot": slot,
                "label": "Push to talk" if slot == PTT else f"Contact {slot}",
                **self._test_events.get(slot, empty),
            }
            for slot in slots
        ]
