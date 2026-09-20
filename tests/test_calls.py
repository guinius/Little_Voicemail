"""The call state machine: hold-to-call, answering, ending, and what
happens when a different contact button is pressed by accident while one
of the three call states (CALLING/RINGING/IN_CALL) is active.

Mirrors test_app_flow.py's conventions: a fake for the one I/O dependency
under test (FakeCallClient, alongside FakeAudio/FakeSignal), driving
PhoneApp directly rather than any real hardware or network.
"""

import asyncio
import time
from pathlib import Path

import pytest

from src.app import CALL_HOLD_SECONDS, PhoneApp, State
from src.config import Config
from src.hardware import Hardware
from src.hardware.buttons import PTT, Action, ButtonEvent, ButtonReader
from src.hardware.leds import LedController
from src.messages import MessageQueue

GRANDMA = "111222333"
UNCLE = "444555666"


class FakeAudio:
    """As test_app_flow.py's FakeAudio, plus a ring counter so a test can
    tell a ring loop is actually going, not just that play() was called
    once. Includes start/stop_recording even though no test here exercises
    recording directly, because PTT is now overloaded (record vs. hang up)
    and one test presses it with no call in progress to prove the ordinary
    voice-note path is untouched."""

    def __init__(self, sounds_dir=None):
        self.sounds_dir = sounds_dir
        self.played: list[str] = []
        self.ringtones = 0
        self.stopped = 0
        self.recording = False

    async def start_recording(self):
        self.recording = True
        return Path("/tmp/fake.wav")

    async def stop_recording(self):
        from src.audio import Recording

        self.recording = False
        return Recording(path=Path("/tmp/fake.wav"), duration=3.0, aborted=False)

    async def encode_voice_note(self, path):
        return Path("/tmp/fake.m4a")

    async def play(self, path, volume=1.0):
        self.played.append(str(path))
        return True

    async def play_ringtone(self) -> None:
        self.ringtones += 1
        # A real ring loops _ring_loop() calling this repeatedly with a
        # short sleep between - give the test loop a chance to interleave
        # (cancel it) rather than spinning hot.
        await asyncio.sleep(0.01)

    async def stop_playback(self) -> None:
        self.stopped += 1


class FakeSignal:
    def __init__(self):
        self.connected = True
        self.on_voice_message = None
        self.on_read_receipt = None

    def start(self):
        pass

    async def stop(self):
        pass


class FakeCallClient:
    def __init__(self):
        self.connected = True
        self.calls_made: list[str] = []
        self.answered = 0
        self.declined = 0
        self.hung_up = 0
        self.fail_next_call = False
        self.fail_next_answer = False
        self.on_incoming_call = None
        self.on_call_connected = None
        self.on_call_ended = None

    def start(self):
        pass

    async def stop(self):
        pass

    async def call(self, telegram_id: str) -> None:
        if self.fail_next_call:
            self.fail_next_call = False
            raise RuntimeError("could not place call")
        self.calls_made.append(telegram_id)

    async def answer(self) -> None:
        if self.fail_next_answer:
            self.fail_next_answer = False
            raise RuntimeError("could not answer")
        self.answered += 1

    async def hang_up(self) -> None:
        self.hung_up += 1

    async def decline(self) -> None:
        self.declined += 1


@pytest.fixture
async def env(tmp_path):
    config = Config(tmp_path / "config.json")
    config.set_contact(1, "Grandma", "", telegram_id=GRANDMA)
    config.set_contact(2, "Uncle", "", telegram_id=UNCLE)

    hardware = Hardware(
        buttons=ButtonReader(expander=None, live_hardware=False),
        leds=LedController(expander=None, live_hardware=False),
        live=False,
    )
    hardware.leds.start()
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    (sounds_dir / "chime.wav").write_bytes(b"RIFF")
    audio = FakeAudio(sounds_dir=sounds_dir)
    signal = FakeSignal()
    calls = FakeCallClient()
    queue = MessageQueue(tmp_path / "messages.db")
    app = PhoneApp(
        config, hardware, audio, signal, queue, calls=calls,
        test_mode_flag_path=tmp_path / "test_mode.flag",
    )
    try:
        yield app, config, audio, calls, queue
    finally:
        await hardware.leds.stop()
        queue.close()


async def press(app, slot):
    await app._handle_button(ButtonEvent(slot, Action.PRESS, 0.0))


async def ptt_down(app):
    await app._handle_button(ButtonEvent(PTT, Action.PRESS, 0.0))


def hold(app, slot, seconds=CALL_HOLD_SECONDS + 0.1):
    """Make the real ButtonReader report `slot` as having been held for
    `seconds` - what _check_call_hold reads via held_since()."""
    app.hw.buttons._pressed_at[slot] = time.monotonic() - seconds


async def run_pending_tasks(app):
    """Let any asyncio.create_task()'d call/ring work started this tick
    actually run - the same reason test_app_flow.py's send tests await
    app.wait_for_send()."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# -- placing a call ----------------------------------------------------------


@pytest.mark.asyncio
async def test_holding_a_selected_contact_for_3s_places_a_call(env):
    app, config, audio, calls, queue = env
    await press(app, 1)
    assert app.state is State.SELECTED

    hold(app, 1)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)

    assert app.state is State.CALLING
    assert app._call_slot == 1
    assert calls.calls_made == [GRANDMA]
    assert app.hw.leds._patterns[1].kind == "blink"


@pytest.mark.asyncio
async def test_a_hold_shorter_than_3s_does_not_call(env):
    app, config, audio, calls, queue = env
    await press(app, 1)

    hold(app, 1, seconds=CALL_HOLD_SECONDS - 0.5)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)

    assert app.state is State.SELECTED
    assert calls.calls_made == []


@pytest.mark.asyncio
async def test_holding_an_unassigned_contact_does_not_call(env):
    app, config, audio, calls, queue = env
    # Slot 3 is unassigned - pressing it does nothing, so drive the hold
    # check directly with a fabricated selection to prove _check_call_hold
    # itself guards on a callable contact, not just relying on the press
    # already having been rejected.
    app.selected_slot = 3
    app.state = State.SELECTED

    hold(app, 3)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)

    assert app.state is State.SELECTED
    assert calls.calls_made == []


@pytest.mark.asyncio
async def test_a_contact_with_no_telegram_id_is_not_called(env):
    app, config, audio, calls, queue = env
    config.set_contact(3, "Dad", "+447700900999")  # Signal only, no telegram_id
    await press(app, 3)

    hold(app, 3)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)

    assert app.state is State.SELECTED
    assert calls.calls_made == []


@pytest.mark.asyncio
async def test_calling_is_a_no_op_when_no_call_client_is_configured(env):
    app, config, audio, calls, queue = env
    app.calls = None
    await press(app, 1)

    hold(app, 1)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)

    assert app.state is State.SELECTED  # normal voice-note selection, untouched


@pytest.mark.asyncio
async def test_a_failed_dial_returns_to_idle(env):
    app, config, audio, calls, queue = env
    calls.fail_next_call = True
    await press(app, 1)

    hold(app, 1)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)

    assert app.state is State.IDLE
    assert app._last_error is not None


# -- ending a call with push-to-talk -----------------------------------------


@pytest.mark.asyncio
async def test_ptt_cancels_an_outgoing_call_before_it_is_answered(env):
    app, config, audio, calls, queue = env
    await press(app, 1)
    hold(app, 1)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)
    assert app.state is State.CALLING

    await ptt_down(app)

    assert app.state is State.IDLE
    assert calls.hung_up == 1
    assert app.hw.leds._patterns[1].kind == "off"


@pytest.mark.asyncio
async def test_ptt_ends_a_connected_call(env):
    app, config, audio, calls, queue = env
    await press(app, 1)
    hold(app, 1)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)
    await app._on_call_connected()
    assert app.state is State.IN_CALL

    await ptt_down(app)

    assert app.state is State.IDLE
    assert calls.hung_up == 1


@pytest.mark.asyncio
async def test_ptt_while_recording_still_finishes_the_recording_not_a_call(env):
    """PTT is heavily overloaded now (record vs. hang up) - make sure the
    ordinary voice-note path (no call in progress) is completely untouched."""
    app, config, audio, calls, queue = env
    await press(app, 2)
    await ptt_down(app)
    assert app.state is State.RECORDING
    assert calls.hung_up == 0


# -- receiving a call ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_incoming_call_rings_and_blinks_that_contacts_lamp(env):
    app, config, audio, calls, queue = env

    await app._on_incoming_call(GRANDMA)
    await run_pending_tasks(app)

    assert app.state is State.RINGING
    assert app._call_slot == 1
    assert app.hw.leds._patterns[1].kind == "blink"
    for other in (2, 3, 4, 5, 6):
        assert app.hw.leds._patterns[other].kind == "off"
    assert audio.ringtones >= 1

    app._ring_task.cancel()  # tidy up the still-running ring loop


@pytest.mark.asyncio
async def test_pressing_the_ringing_contacts_own_button_answers(env):
    app, config, audio, calls, queue = env
    await app._on_incoming_call(GRANDMA)
    await run_pending_tasks(app)

    await press(app, 1)

    assert app.state is State.IN_CALL
    assert calls.answered == 1
    assert audio.stopped >= 1  # the ring loop was cut off
    assert app.hw.leds._patterns[1].kind == "solid"


@pytest.mark.asyncio
async def test_a_different_button_pressed_while_ringing_is_ignored(env):
    """The behaviour asked for explicitly: an accidental press on any
    button other than the one ringing does nothing at all - it neither
    answers, nor declines, nor redirects the call to that other contact.
    The call just keeps ringing until the right button is pressed, it
    times out, or push-to-talk cancels it."""
    app, config, audio, calls, queue = env
    await app._on_incoming_call(GRANDMA)
    await run_pending_tasks(app)

    await press(app, 2)  # Uncle's button, not Grandma's

    assert app.state is State.RINGING
    assert app._call_slot == 1
    assert calls.answered == 0
    assert calls.declined == 0
    assert calls.hung_up == 0
    assert app.hw.leds._patterns[1].kind == "blink"  # Grandma's lamp: undisturbed
    assert app.hw.leds._patterns[2].kind == "off"    # Uncle's own lamp: not lit either

    app._ring_task.cancel()


@pytest.mark.asyncio
async def test_a_different_button_pressed_during_a_connected_call_is_ignored(env):
    app, config, audio, calls, queue = env
    await app._on_incoming_call(GRANDMA)
    await run_pending_tasks(app)
    await press(app, 1)
    assert app.state is State.IN_CALL

    await press(app, 2)

    assert app.state is State.IN_CALL  # the call is not dropped or switched
    assert app._call_slot == 1
    assert calls.hung_up == 0


@pytest.mark.asyncio
async def test_an_incoming_call_from_an_unknown_contact_is_declined(env):
    app, config, audio, calls, queue = env

    await app._on_incoming_call("999888777")

    assert app.state is State.IDLE
    assert calls.declined == 1


@pytest.mark.asyncio
async def test_an_incoming_call_during_quiet_time_is_declined_without_ringing(env):
    app, config, audio, calls, queue = env
    windows = config.get("quiet_times")
    for w in windows:
        w["enabled"] = True
        w["start"] = "00:00"
        w["end"] = "23:59"
        w["days"] = [0, 1, 2, 3, 4, 5, 6]
    config.set(windows, "quiet_times")

    await app._on_incoming_call(GRANDMA)

    assert app.state is State.IDLE
    assert calls.declined == 1
    assert audio.ringtones == 0


@pytest.mark.asyncio
async def test_an_incoming_call_while_busy_is_declined(env):
    app, config, audio, calls, queue = env
    app.state = State.RECORDING  # mid something else already

    await app._on_incoming_call(GRANDMA)

    assert app.state is State.RECORDING  # untouched
    assert calls.declined == 1


# -- timeouts -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unanswered_incoming_call_times_out(env):
    app, config, audio, calls, queue = env
    config.set(1, "telegram", "calling", "ring_timeout_seconds")
    await app._on_incoming_call(GRANDMA)
    await run_pending_tasks(app)
    assert app.state is State.RINGING

    await app._check_call_timeouts(app._call_started + 2.0)

    assert app.state is State.IDLE
    assert calls.hung_up == 1


@pytest.mark.asyncio
async def test_an_unanswered_outgoing_call_times_out(env):
    app, config, audio, calls, queue = env
    config.set(1, "telegram", "calling", "dial_timeout_seconds")
    await press(app, 1)
    hold(app, 1)
    app._check_call_hold(time.monotonic())
    await run_pending_tasks(app)
    assert app.state is State.CALLING

    await app._check_call_timeouts(app._call_started + 2.0)

    assert app.state is State.IDLE
    assert calls.hung_up == 1


# -- the far end hanging up first ---------------------------------------------


@pytest.mark.asyncio
async def test_the_far_end_hanging_up_ends_the_call_here_too(env):
    app, config, audio, calls, queue = env
    await app._on_incoming_call(GRANDMA)
    await run_pending_tasks(app)
    await press(app, 1)
    assert app.state is State.IN_CALL

    await app._on_call_ended_remotely("hung up")

    assert app.state is State.IDLE


# -- quiet time starting mid-call ---------------------------------------------


@pytest.mark.asyncio
async def test_quiet_time_starting_mid_call_ends_it(env):
    app, config, audio, calls, queue = env
    await app._on_incoming_call(GRANDMA)
    await run_pending_tasks(app)
    await press(app, 1)
    assert app.state is State.IN_CALL

    windows = config.get("quiet_times")
    for w in windows:
        w["enabled"] = True
        w["start"] = "00:00"
        w["end"] = "23:59"
        w["days"] = [0, 1, 2, 3, 4, 5, 6]
    config.set(windows, "quiet_times")
    app._was_quiet = False  # force the edge-detection in _check_quiet_time_transition to fire

    await app._check_quiet_time_transition()

    assert app.state is State.IDLE
    assert calls.hung_up == 1
