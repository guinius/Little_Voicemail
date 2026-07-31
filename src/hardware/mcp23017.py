"""Minimal MCP23017 I2C GPIO expander driver.

Ten illuminated buttons need twenty lines in total, so buttons and LEDs
both live on MCP23017 expanders rather than on the Pi's own GPIO. The
expanders are effectively free: the ReSpeaker HAT already occupies I2C for
codec control, so hanging them off SDA/SCL costs no additional pins. Going
direct to GPIO would spend all twenty remaining lines and still need driver
arrays, because the Pi's total GPIO budget (~50 mA) is well below what ten
lamps draw. See HARDWARE.md for the full reasoning.

    Expander A (0x20)  ten inputs  - buttons, pulled up, interrupt on change
    Expander B (0x21)  ten outputs - LEDs, driven through ULN2803 arrays

Only the register set this project needs is implemented. Banked addressing
is left at its power-on default (IOCON.BANK = 0), so A/B register pairs are
adjacent and 16-bit accesses work as little-endian word reads.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Register addresses with IOCON.BANK = 0.
IODIRA, IODIRB = 0x00, 0x01
IPOLA, IPOLB = 0x02, 0x03
GPINTENA, GPINTENB = 0x04, 0x05
DEFVALA, DEFVALB = 0x06, 0x07
INTCONA, INTCONB = 0x08, 0x09
IOCON = 0x0A
GPPUA, GPPUB = 0x0C, 0x0D
INTFA, INTFB = 0x0E, 0x0F
INTCAPA, INTCAPB = 0x10, 0x11
GPIOA, GPIOB = 0x12, 0x13
OLATA, OLATB = 0x14, 0x15

# IOCON bits.
IOCON_MIRROR = 0x40  # tie INTA/INTB together: one Pi pin sees either port
IOCON_ODR = 0x04     # open-drain interrupt output
IOCON_SEQOP = 0x20   # disable sequential auto-increment


class MCP23017:
    """A single expander on the I2C bus."""

    def __init__(self, bus, address: int = 0x20):
        self._bus = bus
        self.address = address

    # -- low level -------------------------------------------------------

    def _read8(self, reg: int) -> int:
        return self._bus.read_byte_data(self.address, reg)

    def _write8(self, reg: int, value: int) -> None:
        self._bus.write_byte_data(self.address, reg, value & 0xFF)

    def _read16(self, reg: int) -> int:
        lo, hi = self._bus.read_i2c_block_data(self.address, reg, 2)
        return (hi << 8) | lo

    def _write16(self, reg: int, value: int) -> None:
        self._bus.write_i2c_block_data(
            self.address, reg, [value & 0xFF, (value >> 8) & 0xFF]
        )

    # -- configuration ---------------------------------------------------

    def configure_inputs(self, mask: int, pullup: bool = True,
                         interrupt: bool = True) -> None:
        """Set every pin in `mask` (bit 0 = GPA0 .. bit 15 = GPB7) as input."""
        self._write16(IODIRA, mask)
        if pullup:
            self._write16(GPPUA, mask)
        # Interrupt on any change: leave INTCON cleared so the pin is
        # compared against its previous value rather than DEFVAL.
        self._write16(INTCONA, 0x0000)
        self._write16(GPINTENA, mask if interrupt else 0x0000)
        self._write8(IOCON, IOCON_MIRROR | IOCON_ODR)

    def configure_outputs(self, mask: int) -> None:
        """Set every pin in `mask` as an output, driven low (LEDs off)."""
        current = self._read16(IODIRA)
        self._write16(IODIRA, current & ~mask)
        self._write16(OLATA, 0x0000)

    # -- I/O -------------------------------------------------------------

    def read_gpio(self) -> int:
        """Current pin state across both ports as a 16-bit value."""
        return self._read16(GPIOA)

    def read_interrupt_capture(self) -> int:
        """Pin state latched at the moment of the interrupt.

        Reading this also clears the interrupt condition.
        """
        return self._read16(INTCAPA)

    def read_interrupt_flags(self) -> int:
        return self._read16(INTFA)

    def write_gpio(self, value: int) -> None:
        self._write16(OLATA, value)

    def clear_interrupts(self) -> None:
        self._read16(INTCAPA)


class NullBus:
    """Stand-in I2C bus for running the app off-device.

    Reads return all-ones so inputs look idle (buttons are active-low),
    which lets the web UI and state machine be exercised on a laptop.
    """

    def __init__(self):
        self._regs: dict[tuple[int, int], int] = {}

    def read_byte_data(self, addr: int, reg: int) -> int:
        return self._regs.get((addr, reg), 0xFF)

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        self._regs[(addr, reg)] = value

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        return [self._regs.get((addr, reg + i), 0xFF) for i in range(length)]

    def write_i2c_block_data(self, addr: int, reg: int, data: list[int]) -> None:
        for i, value in enumerate(data):
            self._regs[(addr, reg + i)] = value

    def close(self) -> None:
        pass


def open_bus(bus_number: int = 1):
    """Open the Pi's I2C bus, falling back to a null bus off-device."""
    try:
        from smbus2 import SMBus

        return SMBus(bus_number), True
    except (ImportError, FileNotFoundError, PermissionError, OSError) as exc:
        log.warning("I2C unavailable (%s); running with simulated hardware", exc)
        return NullBus(), False
