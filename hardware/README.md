# Button and lamp board

`little-voicemail.kicad_sch` is the schematic for the board that sits between
the Raspberry Pi and the seven front-panel buttons. Open
`little-voicemail.kicad_pro` in KiCad 7 or newer.

It does one job: put six contact buttons and the push-to-talk button — switch
and lamp for each — onto a single MCP23017 at address `0x20`, so the whole
panel costs the Pi nothing but the I²C bus it was already using for the audio
codec. See [HARDWARE.md](../HARDWARE.md) for why it is built this way.

## What is on it

| Ref | Part | Footprint | Notes |
|-----|------|-----------|-------|
| J1 | RPi 40-pin header | `PinSocket_2x20_P2.54mm_Vertical` | Stacking, so the ReSpeaker HAT still fits above |
| U1 | MCP23017 | `DIP-28_W7.62mm` | Port A reads switches, port B sinks lamps |
| C1 | 100 nF | `C_Disc_D5.0mm_W2.5mm_P5.00mm` | Decoupling, keep it close to pins 9/10 |
| R1–R7 | 220 Ω | `R_Axial_DIN0207` | One per lamp, sets LED current |
| J2–J8 | 4-way JST-XH | `JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical` | One per button |

The symbols are defined inside the schematic file itself, so it opens without
needing any library set up. Footprints are stock KiCad ones.

## Netlist

| Net | Connections |
|-----|-------------|
| `+3V3` | J1.1, U1.9 (VDD), U1.18 (RESET), C1.1 |
| `+5V` | J1.2, J1.4, R1–R7 (top) |
| `GND` | J1.6/9/14/20/25/30/34/39, U1.10 (VSS), U1.15–17 (A0/A1/A2), C1.2, J2–J8 pin 2 |
| `SDA` | J1.3 (GPIO2), U1.13 |
| `SCL` | J1.5 (GPIO3), U1.12 |
| `BTN_1`…`BTN_6` | U1.21–26 (GPA0–GPA5) → J2–J7 pin 1 |
| `BTN_PTT` | U1.27 (GPA6) → J8 pin 1 |
| `LAMP_1`…`LAMP_6` | U1.1–6 (GPB0–GPB5) → J2–J7 pin 4 |
| `LAMP_PTT` | U1.7 (GPB6) → J8 pin 4 |
| `LEDA_1`…`LEDA_PTT` | R1–R7 (bottom) → J2–J8 pin 3 |

### Button connector pinout (J2–J8)

```
  1  SW      switch, to GPAn        (internal pull-up; pressed = low)
  2  SW_GND  switch return, to GND
  3  LED_A   LED anode, from +5V via Rn
  4  LED_K   LED cathode, to GPBn   (LOW = lit)
```

Same pinout on all seven, so the looms are interchangeable. J8 is the
push-to-talk button.

## Three things to get right

**The lamps are active low.** Anode to +5 V through the resistor, cathode to
the expander pin. Driving `GPBn` low lights the lamp; the pins idle high.
Wiring a lamp the other way round inverts the whole panel. This is why the
expander sinks rather than sources: it runs at 3.3 V and its output high sags
under load, which is not enough headroom for a white or blue LED at ~3.0 V
forward. Pulling the cathode down against 5 V works for every colour.

**Do not fit I²C pull-ups.** The Pi already has 1.8 kΩ on SDA and SCL. A
second pair in parallel is unnecessary and pulls the bus harder than it needs.

**Tie RESET high.** U1 pin 18 goes to 3V3. Left floating it glitches, and the
symptom looks exactly like phantom button presses.

## Resistor sizing

R1–R7 set the lamp current from the 5 V rail:

| LED colour | Vf | 220 Ω | Seven lamps |
|------------|---:|------:|------------:|
| White / blue | ~3.0 V | 9 mA | 64 mA |
| Green / yellow | ~2.2 V | 13 mA | 91 mA |
| Red | ~2.0 V | 14 mA | 95 mA |

Worst case is ~95 mA against the MCP23017's 150 mA package limit, and 14 mA
against its 25 mA per-pin limit. Both have real margin. Go to 470 Ω if the
lamps are too bright for a bedroom.

If you use buttons with a **pre-wired 5 V LED module** (resistor already
inside, ~20 mA fixed), fit links instead of R1–R7. Seven of those is 140 mA,
which still fits the 150 mA package limit but leaves almost nothing spare.

## Deliberately unconnected

ERC will flag these; they are all intentional.

- **Most of J1.** This board stacks under the ReSpeaker HAT, which needs I²S
  (GPIO 18–21), I²C (2–3) and SPI (7–11). Those pins pass through rather than
  landing anywhere on this board.
- **U1 INTA / INTB** (pins 20, 19). The driver polls at 50 Hz instead of
  chasing the interrupt pin — push-to-talk needs continuous held-state anyway,
  and polling cannot wedge the way a missed interrupt latch can. Bring them
  out to a header if you ever want them.
- **U1 GPA7 / GPB7** (pins 28, 8). Spare — an eighth button, if you decide six
  contacts is not enough.
- **U1 NC** (pins 11, 14).

## Board notes

- Keep C1 within a few millimetres of U1 pins 9 and 10.
- `+5V`, `+3V3` and `GND` are assigned to a `Power` net class at 0.8 mm track
  width. Signal nets carry under 15 mA and are fine at the 0.25 mm default.
- The seven connectors are laid out in schematic order; putting them along one
  board edge in panel order makes the looms much easier to dress.
- Nothing here is speed-sensitive. I²C runs at 100 kHz and the lamps are
  static, so track routing is unconstrained.

## If you change the button count

The firmware derives every bit position from `NUM_CONTACTS` in
`src/config.py`. Port A is switches from GPA0 up, port B is lamps from GPB0
up, and push-to-talk always takes the pin after the last contact. Six contacts
plus push-to-talk uses seven of the eight pins on each port, so one more
contact fits on this expander without any board change beyond an eighth
connector.
