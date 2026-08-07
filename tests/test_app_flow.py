"""End-to-end behaviour of the state machine with fakes for I/O.

These cover the rules a parent would actually notice if they broke:
selection timeout, listen-before-reply, quiet-time lockout, and the
cross-device read receipt clearing a light.
"""

import asyncio
from pathlib import Path

import pytest

from src.app import PhoneApp, State
from src.config import Config
from src.hardware import Hardware
from src.hardware.buttons import PTT, Action, ButtonEvent, ButtonReader
from src.hardware.leds import LedController
from src.messages import MessageQueue
from src.signal_client import IncomingVoiceMessage, ReadReceipt

GRANDMA = "+447700900123"
UNCLE = "+447700900456"


class FakeAudio:
    """Records what would have been recorded, played and sent."""

    def __init__(self, sounds_dir: Path | None = None):
        self.sounds_dir = sounds_dir or Path("/tmp/fake-sounds")
        self.recording = False
        self.played: list[str] = []
        self.ringtones = 0
        self.recorded_duration = 3.0
        self.abort_next = False
        self.fail_next_start = False

    async def start_recording(self):
        if self.fail_next_start:
            self.fail_next_start = False
            from src.audio import AudioError

            raise AudioError("arecord: no such device")
        self.recording = True
        return Path("/tmp/fake.wav")

    async def stop_recording(self):
        from src.audio import Recording

        self.recording = False
        return Recording(
            path=Path("/tmp/fake.wav"),
            duration=self.recorded_duration,
            aborted=self.abort_next,
        )

    async def encode_voice_note(self, path):
        return Path("/tmp/fake.m4a")

    async def play(self, path, volume=1.0):
        self.played.append(str(path))
        return True

    async def play_sequence(self, items, gap=None, on_played=None):
        for item in items:
            self.played.append(str(item.attachment))
            if on_played:
                await on_played(item)
        return len(items)

    async def play_ringtone(self):
        self.ringtones += 1


class FakeSignal:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.receipts: list[tuple[str, int]] = []
        self.connected = True
        self.on_voice_message = None
        self.on_read_receipt = None
        self.fail_next_send = False

    def start(self):
        pass

    async def stop(self):
        pass

    async def send_voice_note(self, recipient, path):
        if self.fail_next_send:
            raise ConnectionError("signal-cli is down")
        self.sent.append((recipient, str(path)))
        return 1234

    async def send_receipt(self, recipient, timestamp, receipt_type="read"):
        self.receipts.append((recipient, timestamp))


@pytest.fixture
async def env(tmp_path):
    config = Config(tmp_path / "config.json")
    config.set_contact(1, "Grandma", GRANDMA)
    config.set_contact(2, "Uncle", UNCLE)

    hardware = Hardware(
        buttons=ButtonReader(expander=None, live_hardware=False),
        leds=LedController(expander=None, live_hardware=False),
        live=False,
    )
    # The render loop has to be running: flash_all waits on it, and the
    # quiet-time path goes through flash_all.
    hardware.leds.start()
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    (sounds_dir / "chime.wav").write_bytes(b"RIFF")
    audio = FakeAudio(sounds_dir=sounds_dir)
    signal = FakeSignal()
    queue = MessageQueue(tmp_path / "messages.db")
    # Not part of the fixture's yielded tuple to avoid reshaping every
    # existing test's unpacking - tests that need it reach app._test_mode_
    # flag_path directly, same as other "private" state already used
    # throughout this file (app.selected_slot, app._selection_expires, ...).
    app = PhoneApp(
        config, hardware, audio, signal, queue,
        test_mode_flag_path=tmp_path / "test_mode.flag",
    )
    try:
        yield app, config, audio, signal, queue
    finally:
        await app.wait_for_send(timeout=5.0)
        await hardware.leds.stop()
        queue.close()


async def press(app, slot):
    await app._handle_button(ButtonEvent(slot, Action.PRESS, 0.0))


async def ptt_down(app):
    await app._handle_button(ButtonEvent(PTT, Action.PRESS, 0.0))


async def ptt_up(app, duration=3.0):
    await app._handle_button(ButtonEvent(PTT, Action.RELEASE, 0.0, duration))


# -- boot chime ------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_chime_plays_when_not_quiet(env):
    app, _, audio, _, _ = env
    await app._play_boot_chime()
    assert audio.played == [str(app.audio.sounds_dir / "chime.wav")]


@pytest.mark.asyncio
async def test_boot_chime_is_skipped_during_quiet_time(env):
    app, config, audio, _, _ = env
    enable_quiet_everywhere(config)

    await app._play_boot_chime()

    assert audio.played == []


@pytest.mark.asyncio
async def test_boot_chime_is_skipped_if_the_file_is_missing(env):
    app, _, audio, _, _ = env
    (app.audio.sounds_dir / "chime.wav").unlink()

    await app._play_boot_chime()  # must not raise

    assert audio.played == []


# -- button test mode -------------------------------------------------------


def test_creating_the_flag_file_enters_test_mode(env):
    app, *_ = env
    app._test_mode_flag_path.write_text("", encoding="utf-8")

    app._poll_test_mode(now=1000.0)

    assert app._test_mode is True


def test_removing_the_flag_file_exits_test_mode(env):
    app, *_ = env
    app._test_mode_flag_path.write_text("", encoding="utf-8")
    app._poll_test_mode(now=1000.0)

    app._test_mode_flag_path.unlink()
    app._poll_test_mode(now=1001.0)

    assert app._test_mode is False


def test_entering_test_mode_clears_an_existing_selection(env):
    app, *_ = env
    app._select(1)

    app._test_mode_flag_path.write_text("", encoding="utf-8")
    app._poll_test_mode(now=1000.0)

    assert app.selected_slot is None
    assert app.state is State.IDLE


@pytest.mark.asyncio
async def test_a_press_in_test_mode_lights_only_its_own_lamp(env):
    app, *_ = env
    app._test_mode_flag_path.write_text("", encoding="utf-8")
    app._poll_test_mode(now=1000.0)

    await press(app, 3)

    assert app.hw.leds._patterns[3].kind == "solid"
    for other in (1, 2, 4, 5, 6):
        assert app.hw.leds._patterns[other].kind == "off"
    assert app.hw.leds._patterns[PTT].kind == "off"


@pytest.mark.asyncio
async def test_a_press_in_test_mode_does_not_select_or_play_pending(env):
    app, _, audio, _, queue = env
    queue.add(slot=1, sender=GRANDMA, signal_ts=1, attachment="/tmp/in.ogg")
    app._test_mode_flag_path.write_text("", encoding="utf-8")
    app._poll_test_mode(now=1000.0)

    await press(app, 1)

    assert app.selected_slot is None
    assert audio.played == []  # normal "listen before reply" never ran


@pytest.mark.asyncio
async def test_ptt_in_test_mode_does_not_record_or_send(env):
    app, _, audio, signal, _ = env
    app._test_mode_flag_path.write_text("", encoding="utf-8")
    app._poll_test_mode(now=1000.0)

    await ptt_down(app)
    await ptt_up(app)
    await app.wait_for_send()

    assert not audio.recording
    assert signal.sent == []
    assert app.hw.leds._patterns[PTT].kind == "off"  # released


@pytest.mark.asyncio
async def test_test_mode_records_the_last_action_per_button(env):
    app, *_ = env
    app._test_mode_flag_path.write_text("", encoding="utf-8")
    app._poll_test_mode(now=1000.0)

    await press(app, 2)

    events = {row["slot"]: row for row in app.status()["test_events"]}
    assert events[2]["action"] == "press"
    assert events[2]["at"]
    assert events[1]["action"] is None  # untouched button still shows blank


def test_test_mode_auto_expires_after_the_safety_timeout(env):
    from src.app import TEST_MODE_MAX_SECONDS

    app, *_ = env
    app._test_mode_flag_path.write_text("", encoding="utf-8")
    app._poll_test_mode(now=1000.0)
    assert app._test_mode is True

    app._poll_test_mode(now=1000.0 + TEST_MODE_MAX_SECONDS + 1)

    assert app._test_mode is False
    assert not app._test_mode_flag_path.exists()  # web UI's toggle reflects it too


@pytest.mark.asyncio
async def test_a_stale_flag_file_from_a_previous_run_does_not_survive_startup(env):
    """The flag file is how the web process tells this one test mode
    should be on - but a leftover from an unclean previous exit (crash,
    power cut) must not make a fresh boot come up already in test mode
    with no parent watching. Exercises run() itself, not just the unlink
    call in isolation, since what matters is that the cleanup is actually
    wired into startup."""
    app, *_ = env
    app._test_mode_flag_path.write_text("", encoding="utf-8")

    task = asyncio.create_task(app.run())
    try:
        await asyncio.sleep(0)  # let run() reach past its startup cleanup
        assert not app._test_mode_flag_path.exists()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# -- selection -----------------------------------------------------------


@pytest.mark.asyncio
async def test_pressing_a_contact_selects_it(env):
    app, *_ = env
    await press(app, 1)
    assert app.state is State.SELECTED
    assert app.selected_slot == 1


@pytest.mark.asyncio
async def test_pressing_an_empty_slot_does_nothing(env):
    app, *_ = env
    await press(app, 5)
    assert app.state is State.IDLE
    assert app.selected_slot is None


@pytest.mark.asyncio
async def test_pressing_the_same_contact_again_deselects(env):
    app, *_ = env
    await press(app, 1)
    await press(app, 1)
    assert app.selected_slot is None


@pytest.mark.asyncio
async def test_pressing_another_contact_moves_the_selection(env):
    app, *_ = env
    await press(app, 1)
    await press(app, 2)
    assert app.selected_slot == 2


# -- recording and sending -----------------------------------------------


@pytest.mark.asyncio
async def test_full_send_flow(env):
    app, _, audio, signal, _ = env
    await press(app, 1)
    await ptt_down(app)
    assert app.state is State.RECORDING
    assert audio.recording

    await ptt_up(app)
    await app.wait_for_send()

    assert signal.sent == [(GRANDMA, "/tmp/fake.m4a")]
    assert app.selected_slot is None  # selection released after sending


@pytest.mark.asyncio
async def test_push_to_talk_with_nothing_selected_is_ignored(env):
    app, _, audio, signal, _ = env
    await ptt_down(app)
    assert not audio.recording
    await ptt_up(app)
    assert signal.sent == []


@pytest.mark.asyncio
async def test_a_stab_at_the_button_is_not_sent(env):
    """Too short to be speech - discard rather than send a click."""
    app, _, audio, signal, _ = env
    audio.abort_next = True

    await press(app, 1)
    await ptt_down(app)
    await ptt_up(app, duration=0.2)
    await asyncio.sleep(0)

    assert signal.sent == []
    assert app.selected_slot == 1  # still selected, they can try again


@pytest.mark.asyncio
async def test_selection_does_not_lapse_mid_sentence(env):
    """The 30s timer must not snatch the contact away while recording."""
    app, *_ = env
    await press(app, 1)
    await ptt_down(app)
    assert app._selection_expires == 0.0


@pytest.mark.asyncio
async def test_a_failed_send_does_not_wedge_the_device(env):
    app, _, audio, signal, _ = env
    signal.fail_next_send = True

    await press(app, 1)
    await ptt_down(app)
    await ptt_up(app)
    await app.wait_for_send()

    assert signal.sent == []
    assert app.state is State.IDLE


@pytest.mark.asyncio
async def test_a_failed_send_is_visible_on_the_status_page(env):
    """The System page reads status() to show a parent *why* the lamp
    flashed - a failure that only shows up in journalctl doesn't help
    someone without an SSH session."""
    app, _, audio, signal, _ = env
    signal.fail_next_send = True

    await press(app, 1)
    await ptt_down(app)
    await ptt_up(app)
    await app.wait_for_send()

    status = app.status()
    assert "signal-cli is down" in status["last_error"]
    assert status["last_error_at"]


@pytest.mark.asyncio
async def test_a_successful_send_clears_a_previous_error(env):
    app, _, audio, signal, _ = env
    signal.fail_next_send = True
    await press(app, 1)
    await ptt_down(app)
    await ptt_up(app)
    await app.wait_for_send()
    assert app.status()["last_error"]

    signal.fail_next_send = False
    await press(app, 1)
    await ptt_down(app)
    await ptt_up(app)
    await app.wait_for_send()

    assert app.status()["last_error"] is None


@pytest.mark.asyncio
async def test_a_failed_recording_start_is_also_recorded(env):
    app, _, audio, _, _ = env
    audio.fail_next_start = True

    await press(app, 1)
    await ptt_down(app)

    assert app.state is not State.RECORDING
    assert "arecord" in app.status()["last_error"]


# -- receiving -----------------------------------------------------------


@pytest.mark.asyncio
async def test_incoming_message_queues_and_rings(env):
    app, _, audio, _, queue = env
    await app._on_voice_message(
        IncomingVoiceMessage(GRANDMA, 1000, Path("/tmp/in.ogg"), "audio/ogg")
    )
    assert queue.pending_counts() == {1: 1}
    assert audio.ringtones == 1


@pytest.mark.asyncio
async def test_message_from_a_stranger_is_dropped(env):
    app, _, audio, _, queue = env
    await app._on_voice_message(
        IncomingVoiceMessage("+15550001111", 1000, Path("/tmp/x.ogg"), "audio/ogg")
    )
    assert queue.total_pending() == 0
    assert audio.ringtones == 0


@pytest.mark.asyncio
async def test_you_must_listen_before_you_can_reply(env):
    """Requirement 6: the first press plays, it does not select."""
    app, _, audio, signal, queue = env
    await app._on_voice_message(
        IncomingVoiceMessage(GRANDMA, 1000, Path("/tmp/in.ogg"), "audio/ogg")
    )

    await press(app, 1)

    assert audio.played == ["/tmp/in.ogg"]
    assert queue.pending_counts() == {}
    assert signal.receipts == [(GRANDMA, 1000)]


@pytest.mark.asyncio
async def test_listening_does_not_auto_select_for_reply(env):
    """Listening plays the message and leaves the button idle - the child
    presses it again to select and reply, the same as any other press.
    Auto-selecting made it too easy to hold PTT and reply to whoever was
    last played without meaning to."""
    app, _, audio, _, queue = env
    await app._on_voice_message(
        IncomingVoiceMessage(GRANDMA, 1000, Path("/tmp/in.ogg"), "audio/ogg")
    )

    await press(app, 1)

    assert audio.played == ["/tmp/in.ogg"]
    assert app.selected_slot is None
    assert app.state is State.IDLE


@pytest.mark.asyncio
async def test_a_second_press_after_listening_selects_for_reply(env):
    app, _, audio, _, queue = env
    await app._on_voice_message(
        IncomingVoiceMessage(GRANDMA, 1000, Path("/tmp/in.ogg"), "audio/ogg")
    )
    await press(app, 1)  # plays, no longer selects

    await press(app, 1)  # nothing pending now, so this one selects

    assert app.selected_slot == 1


@pytest.mark.asyncio
async def test_several_messages_play_in_order(env):
    app, _, audio, _, queue = env
    for ts, name in [(3000, "c"), (1000, "a"), (2000, "b")]:
        await app._on_voice_message(
            IncomingVoiceMessage(GRANDMA, ts, Path(f"/tmp/{name}.ogg"), "audio/ogg")
        )

    await press(app, 1)

    assert audio.played == ["/tmp/a.ogg", "/tmp/b.ogg", "/tmp/c.ogg"]


@pytest.mark.asyncio
async def test_reading_on_a_phone_clears_the_light(env):
    """Requirement 14, via Signal's cross-device read receipts."""
    app, _, _, _, queue = env
    await app._on_voice_message(
        IncomingVoiceMessage(GRANDMA, 1000, Path("/tmp/in.ogg"), "audio/ogg")
    )
    assert queue.pending_counts() == {1: 1}

    await app._on_read_receipt(ReadReceipt(sender=GRANDMA, up_to_timestamp=1000))

    assert queue.pending_counts() == {}


# -- quiet time ----------------------------------------------------------


def enable_quiet_everywhere(config):
    config.set(
        [
            {
                "id": "always",
                "label": "Always",
                "enabled": True,
                "start": "00:00",
                "end": "23:59",
                "days": list(range(7)),
            }
        ],
        "quiet_times",
    )


@pytest.mark.asyncio
async def test_quiet_time_blocks_selection(env):
    app, config, *_ = env
    enable_quiet_everywhere(config)

    await press(app, 1)

    assert app.selected_slot is None
    assert app.state is State.IDLE


@pytest.mark.asyncio
async def test_quiet_time_silences_the_ringtone_but_keeps_the_message(env):
    """Requirement 12: it arrives silently and shows up afterwards."""
    app, config, audio, _, queue = env
    enable_quiet_everywhere(config)

    await app._on_voice_message(
        IncomingVoiceMessage(GRANDMA, 1000, Path("/tmp/in.ogg"), "audio/ogg")
    )

    assert audio.ringtones == 0
    assert queue.pending_counts() == {1: 1}


@pytest.mark.asyncio
async def test_quiet_time_blocks_recording(env):
    app, config, audio, signal, _ = env
    enable_quiet_everywhere(config)

    await ptt_down(app)
    await ptt_up(app)

    assert not audio.recording
    assert signal.sent == []
