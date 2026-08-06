"""Hardware abstraction for the Little Voicemail box.

TEMPORARY BRANCH VARIANT: this build runs without the MCP23017 - switches
and lamps go straight onto the Pi header instead of the expander, because
the real chip hasn't arrived yet. See gpio_direct.py and HARDWARE.md
("Testing without the MCP23017") for the pin table and the power-budget
tradeoffs. buttons.py and leds.py are unchanged; they only ever call the
four-method interface (configure_inputs/configure_outputs/read_gpio/
write_gpio) that both mcp23017.MCP23017 and gpio_direct.DirectGPIO
implement, so swapping the backend here is the only change needed. Revert
this file to go back to the expander once it arrives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .buttons import PTT, Action, ButtonEvent, ButtonReader
from .gpio_direct import open_gpio
from .leds import LedController, blink, solid

log = logging.getLogger(__name__)

__all__ = [
    "PTT",
    "Action",
    "ButtonEvent",
    "ButtonReader",
    "LedController",
    "Hardware",
    "blink",
    "solid",
]


@dataclass
class Hardware:
    """Bundles the GPIO backend and the readers/controllers on top."""

    buttons: ButtonReader
    leds: LedController
    live: bool
    _gpio: object = None

    @classmethod
    def create(cls) -> "Hardware":
        gpio, live = open_gpio()
        return cls(
            buttons=ButtonReader(gpio, live_hardware=live),
            leds=LedController(gpio, live_hardware=live),
            live=live,
            _gpio=gpio,
        )

    def start(self) -> None:
        # Lamps first: configure_outputs() sets the lamp pins idling high
        # (dark) before the button reader touches the switch pins, so
        # nothing flashes on the way up.
        self.leds.start()
        self.buttons.start()

    async def stop(self) -> None:
        await self.buttons.stop()
        await self.leds.stop()
        close = getattr(self._gpio, "close", None)
        if callable(close):
            close()
