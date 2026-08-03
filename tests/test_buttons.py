"""Debounce and hold detection, driven with synthetic samples.

The reader's _tick() takes a raw expander word and a timestamp, so the whole
state machine can be exercised without an I2C bus or a running event loop.
"""

import asyncio

import pytest

from src.config import NUM_CONTACTS
from src.hardware.buttons import (
    BUTTON_MASK,
    CONTACT_BITS,
    DEBOUNCE_SECONDS,
    HOLD_THRESHOLD,
    PTT,
    PTT_BIT,
    Action,
    ButtonReader,
)

RELEASED = BUTTON_MASK


def pressed(*slots) -> int:
    """Raw word with the given slots held down (active low)."""
    word = BUTTON_MASK
    for slot in slots:
        bit = PTT_BIT if slot == PTT else CONTACT_BITS[slot]
        word &= ~(1 << bit)
    return word & BUTTON_MASK


@pytest.fixture
def reader():
    # asyncio.Queue needs a loop present at construction on older versions.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield ButtonReader(expander=None, live_hardware=False)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def drain(reader):
    events = []
    while not reader.events.empty():
        events.append(reader.events.get_nowait())
    return events


def test_clean_press_emits_one_press_event(reader):
    reader._tick(pressed(3), 0.0)
    reader._tick(pressed(3), DEBOUNCE_SECONDS + 0.01)

    events = drain(reader)
    assert len(events) == 1
    assert events[0].slot == 3
    assert events[0].action is Action.PRESS


def test_bounce_shorter_than_the_window_is_swallowed(reader):
    """Contact chatter must not register as a press."""
    reader._tick(pressed(3), 0.000)
    reader._tick(RELEASED, 0.005)
    reader._tick(pressed(3), 0.010)
    reader._tick(RELEASED, 0.015)
    reader._tick(RELEASED, 0.015 + DEBOUNCE_SECONDS + 0.01)

    assert drain(reader) == []


def test_press_then_release_reports_duration(reader):
    reader._tick(pressed(5), 0.0)
    reader._tick(pressed(5), DEBOUNCE_SECONDS + 0.01)
    drain(reader)

    reader._tick(RELEASED, 1.00)
    reader._tick(RELEASED, 1.00 + DEBOUNCE_SECONDS + 0.01)

    # A one-second press also crosses the hold threshold; the app ignores
    # holds on contact buttons, so only the release matters here.
    releases = [e for e in drain(reader) if e.action is Action.RELEASE]
    assert len(releases) == 1
    assert releases[0].duration == pytest.approx(1.0, abs=0.02)


def test_hold_fires_once_while_still_held(reader):
    reader._tick(pressed(PTT), 0.0)
    reader._tick(pressed(PTT), DEBOUNCE_SECONDS + 0.01)
    drain(reader)

    # Keep polling well past the hold threshold.
    for step in range(1, 20):
        reader._tick(pressed(PTT), 0.05 * step + HOLD_THRESHOLD)

    holds = [e for e in drain(reader) if e.action is Action.HOLD]
    assert len(holds) == 1
    assert holds[0].is_ptt


def test_two_buttons_are_tracked_independently(reader):
    reader._tick(pressed(1), 0.0)
    reader._tick(pressed(1), DEBOUNCE_SECONDS + 0.01)
    reader._tick(pressed(1, PTT), 0.20)
    reader._tick(pressed(1, PTT), 0.20 + DEBOUNCE_SECONDS + 0.01)

    slots = {(e.slot, e.action) for e in drain(reader)}
    assert (1, Action.PRESS) in slots
    assert (PTT, Action.PRESS) in slots

    assert reader.is_held(1)
    assert reader.is_held(PTT)


def test_releasing_one_button_leaves_the_other_held(reader):
    reader._tick(pressed(2, PTT), 0.0)
    reader._tick(pressed(2, PTT), DEBOUNCE_SECONDS + 0.01)
    drain(reader)

    reader._tick(pressed(PTT), 0.5)
    reader._tick(pressed(PTT), 0.5 + DEBOUNCE_SECONDS + 0.01)

    releases = [e for e in drain(reader) if e.action is Action.RELEASE]
    assert [(e.slot, e.action) for e in releases] == [(2, Action.RELEASE)]
    assert not reader.is_held(2)
    assert reader.is_held(PTT)


def test_all_contacts_map_to_distinct_bits(reader):
    seen = set()
    for slot in range(1, NUM_CONTACTS + 1):
        word = pressed(slot)
        assert word not in seen
        seen.add(word)
    assert pressed(PTT) not in seen
