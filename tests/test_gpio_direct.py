"""Pin-safety checks for the direct-GPIO branch variant.

The thing most likely to go wrong here is silent: a pin reused between a
switch and a lamp, or a pin that quietly collides with something the
ReSpeaker HAT needs (I2C, I2S, SPI0's onboard RGB LEDs, its own user
button, or the ID EEPROM pins). None of that would show up as a Python
exception, so it is worth pinning down in a test rather than only in prose.
"""

from src.hardware.gpio_direct import ALL_PINS, INPUT_PINS, OUTPUT_PINS, DirectGPIO

# Pins the ReSpeaker 2-Mics HAT claims regardless of what this project's
# software does with them - see HARDWARE.md and gpio_direct.py's docstring.
HAT_ID_EEPROM = {0, 1}
HAT_I2C = {2, 3}
HAT_SPI0_RGB_LEDS = {7, 8, 9, 10, 11}
HAT_I2S_AUDIO = {18, 19, 20, 21}
HAT_USER_BUTTON = {17}
RESPEAKER_RESERVED = (
    HAT_ID_EEPROM | HAT_I2C | HAT_SPI0_RGB_LEDS | HAT_I2S_AUDIO | HAT_USER_BUTTON
)


class FakeGPIOModule:
    """Stands in for RPi.GPIO: records calls, tracks pin state/direction."""

    BCM = "BCM"
    IN, OUT = "IN", "OUT"
    HIGH, LOW = 1, 0
    PUD_UP, PUD_OFF = "PUD_UP", "PUD_OFF"

    def __init__(self):
        self.mode = {}       # pin -> IN/OUT
        self.pull = {}       # pin -> PUD_UP/PUD_OFF
        self.levels = {}     # pin -> HIGH/LOW
        self.cleaned_up = []

    def setup(self, pin, direction, pull_up_down=None, initial=None):
        self.mode[pin] = direction
        if direction == self.IN:
            self.pull[pin] = pull_up_down
        else:
            self.levels[pin] = initial if initial is not None else self.LOW

    def input(self, pin):
        return self.levels.get(pin, self.HIGH)

    def output(self, pin, level):
        self.levels[pin] = level

    def cleanup(self, pins):
        self.cleaned_up.extend(pins)


# -- pin table safety -----------------------------------------------------


def test_seven_switches_and_seven_lamps():
    assert len(INPUT_PINS) == 7
    assert len(OUTPUT_PINS) == 7


def test_no_pin_is_reused_between_switches_and_lamps():
    switch_pins = set(INPUT_PINS.values())
    lamp_pins = set(OUTPUT_PINS.values())
    assert switch_pins & lamp_pins == set()
    assert len(switch_pins) == 7
    assert len(lamp_pins) == 7


def test_no_pin_clashes_with_the_respeaker_hat():
    used = set(ALL_PINS)
    clashes = used & RESPEAKER_RESERVED
    assert clashes == set(), f"reused pins the HAT needs: {sorted(clashes)}"


def test_no_pin_is_the_hat_id_eeprom():
    # Belt and braces: 0/1 are also outside the Pi's usable GPIO range, but
    # spell it out since reusing the EEPROM pins is a different failure mode
    # (bus contention at boot) than reusing a signal pin.
    assert set(ALL_PINS).isdisjoint(HAT_ID_EEPROM)


def test_bit_positions_match_the_mcp23017_word_convention():
    """Inputs sit in bits 0-6 (port A) and outputs in bits 8-14 (port B),
    exactly like buttons.py/leds.py already assume for the expander -
    that convention is what makes this a drop-in replacement."""
    assert set(INPUT_PINS) == set(range(7))
    assert set(OUTPUT_PINS) == {8, 9, 10, 11, 12, 13, 14}


# -- DirectGPIO behaviour ---------------------------------------------------


def test_configure_inputs_only_touches_masked_bits():
    fake = FakeGPIOModule()
    gpio = DirectGPIO(fake)
    mask = (1 << 0) | (1 << 6)  # contact 1 and push-to-talk only
    gpio.configure_inputs(mask, pullup=True)

    assert fake.mode[INPUT_PINS[0]] == fake.IN
    assert fake.mode[INPUT_PINS[6]] == fake.IN
    assert INPUT_PINS[1] not in fake.mode  # untouched


def test_configure_outputs_sets_initial_level():
    fake = FakeGPIOModule()
    gpio = DirectGPIO(fake)
    lamp_mask = sum(1 << bit for bit in OUTPUT_PINS)
    gpio.configure_outputs(lamp_mask, initial=lamp_mask)

    for pin in OUTPUT_PINS.values():
        assert fake.mode[pin] == fake.OUT
        assert fake.levels[pin] == fake.HIGH  # idles high, same as the expander


def test_read_gpio_reflects_pin_state_active_low():
    fake = FakeGPIOModule()
    gpio = DirectGPIO(fake)
    button_mask = sum(1 << bit for bit in INPUT_PINS)
    gpio.configure_inputs(button_mask, pullup=True)

    fake.levels[INPUT_PINS[2]] = fake.LOW  # contact 3 pressed

    word = gpio.read_gpio()
    assert word & (1 << 2) == 0            # pressed -> low
    assert word & (1 << 0)                 # everything else idle -> high


def test_write_gpio_drives_every_configured_pin_from_the_word():
    """write_gpio() is a verbatim replace, same as the expander's OLATB
    write - leds.py owns building the full inverted word every frame, this
    layer just puts each bit on its pin."""
    fake = FakeGPIOModule()
    gpio = DirectGPIO(fake)
    lamp_mask = sum(1 << bit for bit in OUTPUT_PINS)
    gpio.configure_outputs(lamp_mask, initial=lamp_mask)

    gpio.write_gpio(1 << 8)  # only bit 8 set in the word

    assert fake.levels[OUTPUT_PINS[8]] == fake.HIGH
    for bit, pin in OUTPUT_PINS.items():
        if bit != 8:
            assert fake.levels[pin] == fake.LOW


def test_close_cleans_up_only_the_pins_this_object_configured():
    fake = FakeGPIOModule()
    gpio = DirectGPIO(fake)
    gpio.configure_inputs(1 << 0)
    gpio.configure_outputs(1 << 8)

    gpio.close()

    assert set(fake.cleaned_up) == {INPUT_PINS[0], OUTPUT_PINS[8]}
