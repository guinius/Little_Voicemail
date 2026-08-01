"""LED output.

Seven lamps on port B of the same MCP23017 that reads the buttons: GPB0-GPB5
are contacts 1-6 and GPB6 is the push-to-talk lamp. Bit positions are offset
by PORT_B so the whole thing still fits one 16-bit register pair.

The lamps are wired as sinks - anode to +5V through a series resistor,
cathode to the expander pin - so a LOW pin lights the lamp and the pins idle
HIGH. Everything above _write() works in positive logic (a set bit means
lit); _write() is the single place that inverts.

Sinking rather than sourcing is deliberate. The expander runs at 3.3V and
its output high sags under load, which is not enough headroom for a white or
blue LED at ~3.0V forward. Pulling the cathode down against a 5V rail works
for any colour. At ~9-13 mA per lamp depending on colour, seven lamps stay
inside the expander's 150 mA package limit with room to spare, so no
Darlington array is needed.

The MCP23017 has no PWM, so patterns are strictly on/off. A render loop
ticks at 25 Hz and writes the whole 16-bit word in one I2C transaction, so
the number of lit LEDs costs nothing extra.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..config import NUM_CONTACTS
from .buttons import PTT
from .mcp23017 import MCP23017

log = logging.getLogger(__name__)

RENDER_INTERVAL = 0.04
PORT_B = 8  # bit offset of GPB0 within the 16-bit register pair
CONTACT_BITS = {slot: PORT_B + slot - 1 for slot in range(1, NUM_CONTACTS + 1)}
PTT_BIT = PORT_B + NUM_CONTACTS
LED_MASK = ((1 << (NUM_CONTACTS + 1)) - 1) << PORT_B
ALL_CONTACTS = tuple(range(1, NUM_CONTACTS + 1))


@dataclass
class Pattern:
    """How one LED should behave until something replaces it."""

    kind: str = "off"          # off | solid | blink
    period: float = 1.0        # full cycle length for blink
    duty: float = 0.5          # fraction of the cycle spent lit
    started: float = field(default_factory=time.monotonic)

    def is_on(self, now: float) -> bool:
        if self.kind == "solid":
            return True
        if self.kind == "blink":
            phase = ((now - self.started) % self.period) / self.period
            return phase < self.duty
        return False


OFF = Pattern("off")


def solid() -> Pattern:
    return Pattern("solid")


def blink(period: float = 0.8, duty: float = 0.5) -> Pattern:
    return Pattern("blink", period=period, duty=duty)


class LedController:
    """Owns the LED word and renders patterns onto it."""

    def __init__(self, expander: MCP23017, live_hardware: bool = True):
        self._expander = expander
        self._live = live_hardware
        self._patterns: dict[int, Pattern] = {slot: OFF for slot in ALL_CONTACTS}
        self._patterns[PTT] = OFF
        self._override: _Override | None = None
        self._last_word = -1
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._expander is not None:
            # Lamps are active low, so the pins idle high (all dark).
            self._expander.configure_outputs(LED_MASK, initial=LED_MASK)
        self._task = asyncio.create_task(self._run(), name="led-render")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.all_off()
        # Force the write through: the cache may already read 0 if the render
        # loop never ran, and shutdown must actually drive the lamps dark.
        self._last_word = -1
        self._write(0x0000)

    # -- pattern control -------------------------------------------------

    def set(self, slot: int, pattern: Pattern) -> None:
        self._patterns[slot] = pattern

    def off(self, slot: int) -> None:
        self._patterns[slot] = OFF

    def all_off(self) -> None:
        for slot in list(self._patterns):
            self._patterns[slot] = OFF

    def contacts_off(self) -> None:
        for slot in ALL_CONTACTS:
            self._patterns[slot] = OFF

    def apply_contact_states(
        self,
        selected: int | None,
        pending: dict[int, int],
        muted: bool = False,
    ) -> None:
        """Set all six contact lamps from the app's current state.

        Selected wins over pending: while a contact is chosen its lamp is
        steady, even if that same contact also has unheard messages. During
        quiet time every contact lamp is dark regardless of what is waiting.
        """
        for slot in ALL_CONTACTS:
            if muted:
                self._patterns[slot] = OFF
            elif slot == selected:
                self._patterns[slot] = solid()
            elif pending.get(slot):
                self._patterns[slot] = blink(period=1.0, duty=0.4)
            else:
                self._patterns[slot] = OFF

    async def flash_all(self, times: int = 3, on: float = 0.18,
                        off: float = 0.18) -> None:
        """Blink every contact lamp `times`, then restore what was there.

        Used to answer a button press during quiet time (requirement 12).
        Awaiting this returns once the sequence has finished.
        """
        done = asyncio.get_running_loop().create_future()
        self._override = _Override(
            slots=ALL_CONTACTS,
            times=times,
            on=on,
            off=off,
            started=time.monotonic(),
            done=done,
        )
        # Only the render loop can resolve this future. Bound the wait so a
        # dead or unstarted render task cannot wedge the caller forever -
        # that would leave the button handler stuck and the box unusable.
        budget = (on + off) * times + 1.0
        try:
            await asyncio.wait_for(done, timeout=budget)
        except asyncio.TimeoutError:
            log.warning("LED flash did not complete; is the render loop running?")
        finally:
            self._override = None

    # -- rendering -------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(RENDER_INTERVAL)
                self._render(time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("LED render failed; continuing")
                await asyncio.sleep(0.5)

    def _render(self, now: float) -> None:
        override = self._override
        if override is not None:
            word, finished = override.word(now)
            if finished:
                if not override.done.done():
                    override.done.set_result(None)
                self._override = None
                word = self._pattern_word(now)
        else:
            word = self._pattern_word(now)
        self._write(word)

    def _pattern_word(self, now: float) -> int:
        word = 0
        for slot, pattern in self._patterns.items():
            if pattern.is_on(now):
                bit = PTT_BIT if slot == PTT else CONTACT_BITS[slot]
                word |= 1 << bit
        return word

    def _write(self, word: int) -> None:
        word &= LED_MASK
        if word == self._last_word:
            return
        self._last_word = word
        if self._live and self._expander is not None:
            try:
                # Invert: the lamps sink, so a lit lamp is a LOW pin. Port A
                # is all inputs, so zeroing its latch bits here has no effect
                # on the pins and keeps this to one I2C transaction.
                self._expander.write_gpio(~word & LED_MASK)
            except OSError:
                log.exception("I2C write to LED expander failed")


@dataclass
class _Override:
    """A finite blink sequence that pre-empts the normal patterns."""

    slots: tuple[int, ...]
    times: int
    on: float
    off: float
    started: float
    done: asyncio.Future

    def word(self, now: float) -> tuple[int, bool]:
        cycle = self.on + self.off
        elapsed = now - self.started
        if elapsed >= cycle * self.times:
            return 0, True
        lit = (elapsed % cycle) < self.on
        if not lit:
            return 0, False
        word = 0
        for slot in self.slots:
            word |= 1 << (PTT_BIT if slot == PTT else CONTACT_BITS[slot])
        return word, False
