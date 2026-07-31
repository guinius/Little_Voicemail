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

    def __init__(self):
        self.recording = False
        self.played: list[str] = []
        self.ringtones = 0
        self.recorded_duration = 3.0
        self.abort_next = False

    async def start_recording(self):
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
        return Path("/tmp/fake.ogg")

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
    audio = FakeAudio()
    signal = FakeSignal()
    queue = MessageQueue(tmp_path / "messages.db")
    app = PhoneApp(config, hardware, audio, signal, queue)
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

    assert signal.sent == [(GRANDMA, "/tmp/fake.ogg")]
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
    assert app.selected_slot == 1  # now they may reply
    assert signal.receipts == [(GRANDMA, 1000)]


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
