"""Lamp bit mapping and active-low drive.

The buttons and the lamps share one expander now, so two things have to hold
that used to be free when they were separate chips: the two masks must not
overlap, and configuring one port must not disturb the other. Both are easy
to break silently, hence these tests.
"""

import time

from src.config import NUM_CONTACTS
from src.hardware.buttons import BUTTON_MASK, PTT
from src.hardware.leds import (
    CONTACT_BITS,
    LED_MASK,
    PORT_B,
    PTT_BIT,
    LedController,
    solid,
)
from src.hardware.mcp23017 import IODIRA, MCP23017, NullBus


class FakeExpander:
    """Records what the controller would put on the bus."""

    def __init__(self):
        self.writes = []
        self.configured_mask = None
        self.configured_initial = None

    def configure_outputs(self, mask, initial=0x0000):
        self.configured_mask = mask
        self.configured_initial = initial

    def write_gpio(self, value):
        self.writes.append(value)


def render(controller) -> int:
    """Force one render and return the word actually written to the bus."""
    controller._render(time.monotonic())
    return controller._expander.writes[-1]


# -- bit layout ---------------------------------------------------------


def test_button_and_lamp_masks_do_not_overlap():
    """Switches live on port A, lamps on port B, on the same chip."""
    assert BUTTON_MASK & LED_MASK == 0
    assert BUTTON_MASK | LED_MASK <= 0xFFFF


def test_lamps_sit_on_port_b():
    for slot in range(1, NUM_CONTACTS + 1):
        assert CONTACT_BITS[slot] >= PORT_B
    assert PTT_BIT >= PORT_B
    assert PTT_BIT == PORT_B + NUM_CONTACTS


def test_every_lamp_maps_to_a_distinct_bit_inside_the_mask():
    bits = {CONTACT_BITS[s] for s in range(1, NUM_CONTACTS + 1)} | {PTT_BIT}
    assert len(bits) == NUM_CONTACTS + 1
    for bit in bits:
        assert LED_MASK & (1 << bit)


# -- active-low drive ---------------------------------------------------


async def test_outputs_idle_high_so_lamps_start_dark():
    fake = FakeExpander()
    controller = LedController(fake, live_hardware=True)
    controller.start()
    try:
        assert fake.configured_mask == LED_MASK
        assert fake.configured_initial == LED_MASK
    finally:
        await controller.stop()

    # Shutdown has to actively drive the pins high, not just stop rendering.
    assert fake.writes[-1] & LED_MASK == LED_MASK


async def test_start_does_not_touch_a_dead_expander():
    """The regression this guards: Hardware.create() hands over a real but
    non-responding chip whenever the startup probe fails (no HAT wired up),
    not a NullBus. Unconditionally writing to it in start() throws OSError
    and crash-loops the whole service on every box without hardware
    attached - live_hardware=False has to mean start() leaves the bus alone.
    """
    fake = FakeExpander()
    controller = LedController(fake, live_hardware=False)
    controller.start()
    try:
        assert fake.configured_mask is None
    finally:
        await controller.stop()


def test_a_lit_lamp_pulls_its_own_pin_low():
    controller = LedController(FakeExpander(), live_hardware=True)
    controller.set(1, solid())
    word = render(controller)

    assert word & (1 << CONTACT_BITS[1]) == 0          # lit -> low
    assert word & (1 << CONTACT_BITS[2])               # dark -> high
    assert word & (1 << PTT_BIT)


def test_ptt_lamp_is_independent_of_the_contacts():
    controller = LedController(FakeExpander(), live_hardware=True)
    controller.set(PTT, solid())
    word = render(controller)

    assert word & (1 << PTT_BIT) == 0
    for slot in range(1, NUM_CONTACTS + 1):
        assert word & (1 << CONTACT_BITS[slot])


def test_all_dark_drives_every_lamp_pin_high():
    controller = LedController(FakeExpander(), live_hardware=True)
    controller.all_off()
    assert render(controller) & LED_MASK == LED_MASK


def test_writes_never_stray_outside_the_lamp_mask():
    """Port A is inputs; the lamp writes must not claim bits outside port B."""
    controller = LedController(FakeExpander(), live_hardware=True)
    controller.set(1, solid())
    controller.set(PTT, solid())
    assert render(controller) & ~LED_MASK == 0


# -- one chip, two ports ------------------------------------------------


def test_configuring_lamps_leaves_the_button_pins_as_inputs():
    """The regression this guards: a whole-register write would break it."""
    expander = MCP23017(NullBus(), 0x20)
    expander.configure_inputs(BUTTON_MASK, pullup=True, interrupt=False)
    expander.configure_outputs(LED_MASK, initial=LED_MASK)

    iodir = expander._read16(IODIRA)
    assert iodir & BUTTON_MASK == BUTTON_MASK   # switches still inputs
    assert iodir & LED_MASK == 0                # lamps now outputs


def test_configuring_buttons_after_lamps_leaves_the_lamps_as_outputs():
    expander = MCP23017(NullBus(), 0x20)
    expander.configure_outputs(LED_MASK, initial=LED_MASK)
    expander.configure_inputs(BUTTON_MASK, pullup=True, interrupt=False)

    iodir = expander._read16(IODIRA)
    assert iodir & LED_MASK == 0
    assert iodir & BUTTON_MASK == BUTTON_MASK
