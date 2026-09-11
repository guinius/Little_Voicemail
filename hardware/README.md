# Little Voicemail HAT

`little-voicemail.kicad_sch` is the schematic for the whole audio and
button/lamp board. Open `little-voicemail.kicad_pro` in KiCad 7 or newer.

It does two jobs on one board:

1. **Audio.** A MAX98357A I2S Class-D amplifier drives the speaker, and two
   ICS-43434 I2S MEMS microphone breakouts (wired in over headers, not bare
   parts — see [HARDWARE.md](../HARDWARE.md#why-digital-i2s-not-a-bigger-analog-amp-bolted-onto-the-same-codec))
   give far-field pickup, all over the Pi's I2S bus.
2. **Buttons and lamps.** Six contact buttons and the push-to-talk button —
   switch and lamp for each — onto a single MCP23017 at address `0x20`, on
   the Pi's I2C bus.

See [HARDWARE.md](../HARDWARE.md) for why it is built this way, including why
this revision replaced the earlier ReSpeaker-HAT-plus-button-board stack.

## What is on it

| Ref | Part | Footprint | Notes |
|-----|------|-----------|-------|
| J1 | RPi 40-pin header | `PinSocket_2x20_P2.54mm_Vertical` | Pin 1 and mounting holes positioned to the official Raspberry Pi HAT mechanical spec |
| U1 | MCP23017 | `DIP-28_W7.62mm` | Port A reads switches, port B sinks lamps |
| U2 | MAX98357A | TQFN-16, 3x3 mm, exposed pad | I2S Class-D amp, drives the speaker directly |
| C1 | 100 nF | `C_Disc_D5.0mm_W2.5mm_P5.00mm` | U1 decoupling, keep it close to pins 9/10 |
| C2 | 100 nF | 0402/0603 ceramic | U2 supply decoupling, close to VDD pins 7/8 |
| C3 | 10 µF | 0805 ceramic or small tantalum | U2 supply bulk decoupling |
| R1–R7 | 220 Ω | `R_Axial_DIN0207` | One per lamp, sets LED current |
| J2–J8 | 4-way JST-XH | `JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical` | One per button |
| J9, J10 | 1x6 2.54 mm pin socket | `PinSocket_1x06_P2.54mm_Vertical` | One per ICS-43434 breakout (left / right) |
| J11 | 2-way JST-PH 2.0 | `JST_PH_S2B-PH-K_1x02_P2.00mm_Vertical` | Speaker |

The symbols are defined inside the schematic file itself, so it opens without
needing any library set up beyond what ships with KiCad.

## Netlist

| Net | Connections |
|-----|-------------|
| `+3V3` | J1.1, J1.17, U1.9 (VDD), U1.18 (RESET), C1.1, J9.1, J10.1 |
| `+5V` | J1.2, J1.4, R1–R7 (top), U2.7/8 (VDD), C2.1, C3.1 |
| `GND` | J1.6/9/14/20/25/30/34/39, U1.10 (VSS), U1.15–17 (A0/A1/A2), C1.2, C2.2, C3.2, U2.3/11/15/17(pad), J2–J8 pin 2, J9.2, J10.2, J9.6 (L select), J11 return |
| `SDA` | J1.3 (GPIO2), U1.13 |
| `SCL` | J1.5 (GPIO3), U1.12 |
| `BCLK` | J1.12 (GPIO18), U2.16, J9.3, J10.3 |
| `LRCLK` | J1.35 (GPIO19), U2.14, J9.4, J10.4 |
| `I2S_DOUT` | J1.40 (GPIO21), U2.1 (DIN) |
| `I2S_DIN` | J1.38 (GPIO20), J9.5, J10.5 (both mics' SD, shared) |
| `MIC_SEL_L` | J9.6 → GND (left slot) |
| `MIC_SEL_R` | J10.6 → +3V3 (right slot) |
| `SPK+` / `SPK-` | U2.9 (OUTP) / U2.10 (OUTN) → J11 |
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

### Mic connector pinout (J9, J10)

Matches the Adafruit ICS-43434 breakout's own header order:

```
  1  3V/VIN  +3V3
  2  GND     GND
  3  SCK     BCLK, shared with the other mic and with U2
  4  WS      LRCLK, shared with the other mic and with U2
  5  SD      shared data line, both mics answer in different TDM slots
  6  SEL     L/R select: GND on J9 (left mic), +3V3 on J10 (right mic)
```

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

- **U1 INTA / INTB** (pins 20, 19). The driver polls at 50 Hz instead of
  chasing the interrupt pin — push-to-talk needs continuous held-state anyway,
  and polling cannot wedge the way a missed interrupt latch can. Bring them
  out to a header if you ever want them.
- **U1 GPA7 / GPB7** (pins 28, 8). Spare. GPB7 is a candidate for a future
  hardware mute on U2's SD_MODE pin — see HARDWARE.md.
- **U1 NC** (pins 11, 14).
- **U2 pins 5, 6, 12, 13** (N.C.) and **GAIN** (pin 2, left floating for the
  default 9 dB gain).

## Board notes

- Keep C1 within a few millimetres of U1 pins 9 and 10, and keep C2/C3 as
  close as practical to U2's VDD pins (7/8) — this matters more for a
  switching Class-D amp than it does for the MCP23017.
- Keep U2 and its speaker traces away from the mic connectors and their
  traces. It's the one layout rule that matters here: a Class-D amp's
  switching edges are exactly the kind of noise a sensitive far-field mic
  picks up if routed alongside it. Give them physical separation, not just a
  shared ground plane.
- `+5V`, `+3V3` and `GND` are assigned to a `Power` net class at 0.8 mm track
  width. Signal nets carry under 15 mA and are fine at the 0.25 mm default.
- The seven button connectors are laid out in schematic order; putting them
  along one board edge in panel order makes the looms much easier to dress.
- Nothing on the button/lamp half is speed-sensitive. I²C runs at 100 kHz and
  the lamps are static. The I2S nets (BCLK/LRCLK/DOUT/DIN) run at audio rates
  (a few MHz for BCLK at typical sample rates) — not RF-fast, but keep them
  reasonably short and away from the amp's speaker output traces regardless.

## If you change the button count

The firmware derives every bit position from `NUM_CONTACTS` in
`src/config.py`. Port A is switches from GPA0 up, port B is lamps from GPB0
up, and push-to-talk always takes the pin after the last contact. Six contacts
plus push-to-talk uses seven of the eight pins on each port, so one more
contact fits on this expander without any board change beyond an eighth
connector.

## What's done, and what KiCad still needs to do

Everything above — every symbol, pin assignment, and net — is in
`little-voicemail.kicad_sch`, cross-checked pin-by-pin against sources named
in HARDWARE.md rather than typed from memory.

**There is no `little-voicemail.kicad_pcb` in this revision.** This session
had no KiCad installation or `kicad-cli` available — no way to generate a
PCB file from the schematic's netlist, place footprints, run ERC/DRC, or
open what I wrote to check it's even valid. A schematic is text I could
reason about and cross-check pin-by-pin against outside sources; a PCB file
is a binary-adjacent format whose correctness (does it even parse, do pads
land where the footprint says, does it pass DRC) is only checkable by
opening it in the tool. Hand-typing one with no way to verify it opens isn't
worth the risk of handing you a board file that looks plausible and doesn't
actually work — better to say plainly it isn't there.

The concrete next step, in KiCad: **File → New PCB**, then
**Tools → Update PCB from Schematic** to pull in every footprint and net
from the schematic above. Two things are fixed by external spec rather than
judgement, so set them first and let everything else reflow around them:

- **Board outline** (Edge.Cuts): 65 x 56.5 mm, per the official [Raspberry Pi
  HAT mechanical spec](https://github.com/raspberrypi/hats).
- **Mounting holes**: 4x M2.5 at (3.5, 3.5), (3.5, 52.5), (61.5, 3.5),
  (61.5, 52.5) mm from the board's bottom-left corner.
- **GPIO header** (J1): centred at (29, 50.5) mm — this is what makes the
  board a HAT rather than just a shield that happens to plug in.

**One more thing to check with ERC before you trust this file fully:** while
adding the I2S nets, I found the header's GPIO18/19/20/21 pins wired to
stray power-rail flags (GND/+5V) instead of signal labels, and fixed those —
they're now the BCLK/LRCLK/I2S_DIN/I2S_DOUT labels this revision needs. I did
**not** touch two other things that looked similarly off but that I
couldn't fully verify by reading the file alone (no KiCad to run ERC and
check what's actually connected): the `SDA`/`SCL` labels near J1 and near
U1 sit at coordinates that, by my reading of the symbol pin tables, land on
GPIO26/NC2/NC1 rather than the real I2C pins (3/5 on J1, 12/13 on U1); and
J1 pin 1 (3V3)'s wire terminates at a GND flag rather than a +3V3 one. These
predate this revision and may simply be things I'm misreading without being
able to render the file — but they're exactly what ERC catches instantly, so
run it before fab rather than trusting the SDA/SCL/3V3 nets as drawn.

After that, placement is a judgement call with one rule worth following
before you route: **keep U2 (MAX98357A) and its speaker traces away from J9
and J10 (the mic connectors) and their traces.** A switching Class-D amp's
edges are exactly the kind of noise a sensitive far-field mic picks up if
routed alongside it — physical separation, not just a shared ground plane.
Route, then run DRC before ordering.
