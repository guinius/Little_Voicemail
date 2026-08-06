"""Direct Raspberry Pi GPIO driver - temporary stand-in for the MCP23017.

For testing everything *other* than the expander while a real MCP23017 is on
order: seven switches and seven lamps wired straight onto the Pi header,
470 Ohm resistor per lamp instead of the expander's 220 Ohm (see
"Testing without the MCP23017" in HARDWARE.md for the full writeup and the
power-budget arithmetic that picked 470 Ohm). This is deliberately close to
the Pi's ~50 mA total GPIO budget - fine for bench testing, not a permanent
substitute for the expander.

buttons.py and leds.py only ever call four methods on whatever object they
are handed - configure_inputs(), configure_outputs(), read_gpio() and
write_gpio() - because that is the whole interface MCP23017 exposes. This
module implements the same four methods against real Pi pins instead of an
I2C expander, so it is a drop-in replacement: nothing downstream needs to
know the switches and lamps moved off the bus. Both sides keep using the
MCP23017 word convention (bit 0 = GPA0 slot .. bit 8+ = GPB0 slot) so the
CONTACT_BITS / PTT_BIT / LED_MASK math in buttons.py and leds.py is
untouched.

Pin choice avoids everything the ReSpeaker HAT needs: I2C (GPIO 2-3, codec
control), I2S (GPIO 18-21, audio), SPI0 (GPIO 7-11, the HAT's own onboard
RGB LEDs - a real electrical clash even though this project never drives
them), GPIO 17 (the HAT's own user button, wired there on the HAT itself)
and GPIO 0-1 (HAT ID EEPROM). That leaves exactly fourteen pins, which is
exactly how many are needed - including GPIO 14/15 (UART), so the serial
console has to be disabled for this variant; see HARDWARE.md.

    Switches (input, internal pull-up, switch to GND - same as GPA on the
    expander):

        Contact 1  GPIO4   (header pin 7)
        Contact 2  GPIO14  (header pin 8)
        Contact 3  GPIO15  (header pin 10)
        Contact 4  GPIO27  (header pin 13)
        Contact 5  GPIO22  (header pin 15)
        Contact 6  GPIO23  (header pin 16)
        Push-to-talk  GPIO24  (header pin 18)

    Lamps (output, active low, cathode to the pin, anode to +5V through a
    470 Ohm resistor - same sinking topology as GPB on the expander):

        Contact 1  GPIO25  (header pin 22)
        Contact 2  GPIO5   (header pin 29)
        Contact 3  GPIO6   (header pin 31)
        Contact 4  GPIO12  (header pin 32)
        Contact 5  GPIO13  (header pin 33)
        Contact 6  GPIO16  (header pin 36)
        Push-to-talk  GPIO26  (header pin 37)
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Bit position (matching the word layout buttons.py/leds.py already use -
# bits 0-6 are port-A/switches, bits 8-14 are port-B/lamps) -> BCM GPIO
# number. Keep in step with the docstring table above if this ever changes.
INPUT_PINS = {0: 4, 1: 14, 2: 15, 3: 27, 4: 22, 5: 23, 6: 24}
OUTPUT_PINS = {8: 25, 9: 5, 10: 6, 11: 12, 12: 13, 13: 16, 14: 26}

ALL_PINS = tuple(INPUT_PINS.values()) + tuple(OUTPUT_PINS.values())


class DirectGPIO:
    """Same four-method surface as MCP23017, backed by real Pi pins.

    read_gpio()/write_gpio() work in the same 16-bit-word, bit-per-pin terms
    the expander used; only configure_inputs()/configure_outputs() need to
    know which bit maps to which physical pin, via INPUT_PINS/OUTPUT_PINS.
    """

    def __init__(self, gpio_module):
        self._gpio = gpio_module
        self._inputs: dict[int, int] = {}   # configured bit -> BCM pin
        self._outputs: dict[int, int] = {}

    def configure_inputs(self, mask: int, pullup: bool = True,
                         interrupt: bool = True) -> None:
        # No hardware interrupt line exists here; buttons.py always polls
        # (see its docstring) and calls this with interrupt=False anyway.
        pull = self._gpio.PUD_UP if pullup else self._gpio.PUD_OFF
        for bit, pin in INPUT_PINS.items():
            if mask & (1 << bit):
                self._gpio.setup(pin, self._gpio.IN, pull_up_down=pull)
                self._inputs[bit] = pin

    def configure_outputs(self, mask: int, initial: int = 0x0000) -> None:
        for bit, pin in OUTPUT_PINS.items():
            if mask & (1 << bit):
                level = self._gpio.HIGH if (initial & (1 << bit)) else self._gpio.LOW
                self._gpio.setup(pin, self._gpio.OUT, initial=level)
                self._outputs[bit] = pin

    def read_gpio(self) -> int:
        # Bits with no configured pin (everything outside port A here) read
        # as 1, same convention NullBus uses, so callers masking with
        # BUTTON_MASK see idle/released rather than a spurious press.
        word = 0xFFFF
        for bit, pin in self._inputs.items():
            if self._gpio.input(pin):
                word |= 1 << bit
            else:
                word &= ~(1 << bit)
        return word

    def write_gpio(self, value: int) -> None:
        for bit, pin in self._outputs.items():
            level = self._gpio.HIGH if (value & (1 << bit)) else self._gpio.LOW
            self._gpio.output(pin, level)

    def close(self) -> None:
        pins = list(self._inputs.values()) + list(self._outputs.values())
        if pins:
            self._gpio.cleanup(pins)


class NullGPIO:
    """Stand-in for running the app off-device; mirrors mcp23017.NullBus."""

    def configure_inputs(self, mask: int, pullup: bool = True,
                         interrupt: bool = True) -> None:
        pass

    def configure_outputs(self, mask: int, initial: int = 0x0000) -> None:
        pass

    def read_gpio(self) -> int:
        return 0xFFFF

    def write_gpio(self, value: int) -> None:
        pass

    def close(self) -> None:
        pass


def open_gpio():
    """Open the Pi's header pins, falling back to a null implementation."""
    try:
        import RPi.GPIO as GPIO

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        return DirectGPIO(GPIO), True
    except (ImportError, RuntimeError) as exc:
        log.warning("GPIO unavailable (%s); running with simulated hardware", exc)
        return NullGPIO(), False
