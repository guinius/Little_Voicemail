"""Hardware abstraction for the Little Voicemail box."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .buttons import PTT, Action, ButtonEvent, ButtonReader
from .leds import LedController, blink, solid
from .mcp23017 import MCP23017, open_bus

log = logging.getLogger(__name__)

# One expander carries the lot: port A reads the seven switches, port B
# sinks the seven lamps. A0/A1/A2 tied to GND gives 0x20.
EXPANDER_ADDR = 0x20

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
    """Bundles the expander and the readers/controllers on top."""

    buttons: ButtonReader
    leds: LedController
    live: bool
    _bus: object = None

    @classmethod
    def create(cls, bus_number: int = 1) -> "Hardware":
        bus, live = open_bus(bus_number)
        chip = MCP23017(bus, EXPANDER_ADDR)
        if live:
            try:
                chip.read_gpio()
            except OSError as exc:
                log.error(
                    "MCP23017 not responding at 0x%02X (%s); "
                    "check wiring and `i2cdetect -y 1`",
                    EXPANDER_ADDR, exc,
                )
                live = False
        return cls(
            buttons=ButtonReader(chip, live_hardware=live),
            leds=LedController(chip, live_hardware=live),
            live=live,
            _bus=bus,
        )

    def start(self) -> None:
        # Lamps first: configure_outputs() sets port B idling high (dark)
        # before the button reader touches port A, so nothing flashes on
        # the way up. Both calls are read-modify-write, so either order is
        # safe - this one is just tidier to watch.
        self.leds.start()
        self.buttons.start()

    async def stop(self) -> None:
        await self.buttons.stop()
        await self.leds.stop()
        close = getattr(self._bus, "close", None)
        if callable(close):
            close()
