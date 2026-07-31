"""Hardware abstraction for the Little Voicemail box."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .buttons import PTT, Action, ButtonEvent, ButtonReader
from .leds import LedController, blink, solid
from .mcp23017 import MCP23017, open_bus

log = logging.getLogger(__name__)

BUTTON_EXPANDER_ADDR = 0x20
LED_EXPANDER_ADDR = 0x21

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
    """Bundles the two expanders and the readers/controllers on top."""

    buttons: ButtonReader
    leds: LedController
    live: bool
    _bus: object = None

    @classmethod
    def create(cls, bus_number: int = 1) -> "Hardware":
        bus, live = open_bus(bus_number)
        button_chip = MCP23017(bus, BUTTON_EXPANDER_ADDR)
        led_chip = MCP23017(bus, LED_EXPANDER_ADDR)
        if live:
            try:
                button_chip.read_gpio()
                led_chip.read_gpio()
            except OSError as exc:
                log.error(
                    "MCP23017 not responding at 0x%02X/0x%02X (%s); "
                    "check wiring and `i2cdetect -y 1`",
                    BUTTON_EXPANDER_ADDR, LED_EXPANDER_ADDR, exc,
                )
                live = False
        return cls(
            buttons=ButtonReader(button_chip, live_hardware=live),
            leds=LedController(led_chip, live_hardware=live),
            live=live,
            _bus=bus,
        )

    def start(self) -> None:
        self.leds.start()
        self.buttons.start()

    async def stop(self) -> None:
        await self.buttons.stop()
        await self.leds.stop()
        close = getattr(self._bus, "close", None)
        if callable(close):
            close()
