"""Debounced button input.

Ten buttons live on one MCP23017: bits 0-8 are contact buttons 1-9 and
bit 9 is the push-to-talk button. They are wired switch-to-ground against
the expander's internal pull-ups, so a pressed button reads 0.

The reader polls at 50 Hz rather than chasing the expander's interrupt pin.
Two I2C word reads per poll is negligible load, push-to-talk needs the
continuous held-state anyway, and polling cannot wedge the way a missed
interrupt latch can.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum

from .mcp23017 import MCP23017

log = logging.getLogger(__name__)

PTT = 0  # slot id reserved for the push-to-talk button
POLL_INTERVAL = 0.02
DEBOUNCE_SECONDS = 0.03
HOLD_THRESHOLD = 0.35  # a press longer than this counts as a hold

CONTACT_BITS = {slot: slot - 1 for slot in range(1, 10)}  # slot 1-9 -> bit 0-8
PTT_BIT = 9
BUTTON_MASK = 0x03FF  # ten inputs


class Action(Enum):
    PRESS = "press"
    RELEASE = "release"
    HOLD = "hold"


@dataclass(frozen=True)
class ButtonEvent:
    slot: int          # 1-9 for contacts, PTT (0) for push-to-talk
    action: Action
    at: float
    duration: float = 0.0

    @property
    def is_ptt(self) -> bool:
        return self.slot == PTT


class ButtonReader:
    """Polls the expander and emits debounced events onto an asyncio queue."""

    def __init__(self, expander: MCP23017, live_hardware: bool = True):
        self._expander = expander
        self._live = live_hardware
        self.events: asyncio.Queue[ButtonEvent] = asyncio.Queue()
        self._stable = BUTTON_MASK      # last debounced raw word (1 = released)
        self._candidate = BUTTON_MASK
        self._candidate_since = 0.0
        self._pressed_at: dict[int, float] = {}
        self._hold_fired: set[int] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._expander is not None:
            self._expander.configure_inputs(BUTTON_MASK, pullup=True, interrupt=False)
        self._task = asyncio.create_task(self._run(), name="button-reader")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def is_held(self, slot: int) -> bool:
        return slot in self._pressed_at

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                self._tick(self._read_raw(), time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("button poll failed; continuing")
                await asyncio.sleep(0.5)

    def _read_raw(self) -> int:
        if not self._live or self._expander is None:
            return BUTTON_MASK
        return self._expander.read_gpio() & BUTTON_MASK

    def _tick(self, raw: int, now: float) -> None:
        """Advance the debounce state machine one poll.

        Split out from _run so tests can drive it with synthetic samples.
        """
        if raw != self._candidate:
            self._candidate = raw
            self._candidate_since = now
        elif raw != self._stable and (now - self._candidate_since) >= DEBOUNCE_SECONDS:
            changed = self._stable ^ raw
            self._stable = raw
            for slot, bit in self._all_bits():
                if changed & (1 << bit):
                    pressed = not (raw & (1 << bit))
                    self._emit_edge(slot, pressed, now)

        # Promote long presses to holds without waiting for the release.
        for slot, since in list(self._pressed_at.items()):
            if slot not in self._hold_fired and (now - since) >= HOLD_THRESHOLD:
                self._hold_fired.add(slot)
                self._put(ButtonEvent(slot, Action.HOLD, now, now - since))

    def _emit_edge(self, slot: int, pressed: bool, now: float) -> None:
        if pressed:
            self._pressed_at[slot] = now
            self._hold_fired.discard(slot)
            self._put(ButtonEvent(slot, Action.PRESS, now))
        else:
            started = self._pressed_at.pop(slot, now)
            self._hold_fired.discard(slot)
            self._put(ButtonEvent(slot, Action.RELEASE, now, now - started))

    @staticmethod
    def _all_bits():
        yield from CONTACT_BITS.items()
        yield PTT, PTT_BIT

    def _put(self, event: ButtonEvent) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("button event queue full; dropped %s", event)
