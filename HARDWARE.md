# Hardware

## Why this combination

The two constraints that drive the whole design:

**No handset.** A push-to-talk box for a small child wants far-field mics, so
they can talk at it from wherever they are standing rather than holding
something to their face. That rules out the USB handset from the Kids-Phone
project and points at a mic array.

**Fourteen GPIO lines.** Six illuminated buttons plus push-to-talk is seven
switches and seven lamps. Fourteen lines is more than the Pi can spare
alongside the audio circuitry once you want to keep a serial console, and
seven lamps draw more current than the Pi's GPIO is allowed to supply. Both
problems are solved by one expander.

Hence: audio and buttons/lamps on one custom board, with buttons and lamps on
a single I2C expander. The next section works through why, because "just use
the Pi's own pins" is the obvious question and at seven buttons the answer is
genuinely close.

## Why the MCP23017 stays

At ten buttons the expanders were unarguable — twenty lines simply did not
exist. At seven the pin count alone no longer settles it, so the reasoning is
worth writing down.

### It costs almost no GPIO budget

The expander hangs off SDA/SCL, and nothing else on this board needs I2C — the
audio chain below is entirely I2S. So the whole button-and-lamp panel costs
two pins (SDA, SCL) versus fourteen direct GPIO. That is the part that settles
it.

### The fourteen pins do exist

Unlike at ten buttons, they genuinely fit. The Pi has 26 usable GPIO
(BCM 2-27; 0 and 1 are the HAT ID EEPROM), and audio's unavoidable claim is
only six of them — I2C (2, 3) for the expander and I2S (18-21) for the codec.
That leaves exactly twenty:

```
4 5 6 7 8 9 10 11 12 13 14 15 16 17 22 23 24 25 26 27
```

Fourteen of twenty, with six spare. You would have to spend SPI0 (7-11) if you
ever wanted it for something else, but you can keep the UART console on
14/15 and stay clear of GPIO 17.

So the pin-count objection is real but no longer decisive. What decides it is
the next section.

### The Pi cannot drive the lamps

Raspberry Pi GPIO is rated **16 mA per pin and roughly 50 mA total across all
pins**. The MCP23017's 150 mA package limit is three times more generous than
the Pi's whole-chip budget.

| Lamps | Each | Seven total | vs the Pi's ~50 mA |
|-------|-----:|------------:|--------------------|
| 30 mm arcade, resistor built in | 20 mA | 140 mA | nearly 3x over |
| Bare LED, 220 ohm from 5 V | 9-13 mA | 63-91 mA | still over |
| Bare LED, ~470 ohm from 5 V | 5 mA | 35 mA | fits, but that is 70% of the entire budget |

Only the dimmest option fits, and it commits most of the Pi's GPIO allowance
to LEDs. On the expander the same lamps draw 63-91 mA against 150 mA, with
per-pin current an order of magnitude inside the 25 mA pin limit.

There is a second, smaller reason. GPIO 9-27 default to pull-down at boot
while 0-8 pull up, and pins get reassigned to ALT functions partway through
startup — so lamps wired straight to Pi GPIO flicker semi-randomly for the
twenty seconds the Pi takes to boot. The expander's outputs are high-Z at
reset and defined the moment the driver configures them, so the panel stays
dark until the app says otherwise. On a device that sits in a child's bedroom
that is worth something.

### Summary

| Approach | Chips | Pi pins spent | Lamp current | Boot flicker | Spare GPIO |
|----------|------:|--------------:|--------------|--------------|-----------:|
| **One MCP23017** (this design) | **1** | **0** | 63-91 mA of 150 mA | no | **20** |
| Direct to GPIO | 0 | 14 | 35 mA of ~50 mA, and dim | yes | 6 |

One chip, zero pins and full brightness beats zero chips, fourteen pins and
dim lamps. The expander is a DIP-28, a decoupling cap and a link to 3V3 — it
is not the complicated part of this build.

## One custom board instead of a HAT-plus-expander stack

The original design stacked a Seeed ReSpeaker 2-Mics Pi HAT (mics, codec, and
a small onboard amp) under a custom MCP23017 button/lamp board. That combination
shipped, worked, and is still a fine way to build this project without a PCB
order. This document now describes replacing it with a **single custom HAT**
that puts the speaker driver, microphones, and the MCP23017 button/lamp
circuit on one board. Two things changed to justify the extra work:

**The onboard amp was the actual ceiling, and it was already maxed out.**
`little-voicemail-audio-levels.service` maxes every playback control the
ReSpeaker's codec exposes on every boot, and the app-level volume defaults
were tuned up separately (see the git history of `tools/set-audio-levels.sh`
and the ringtone/mic-gain commits). None of that can go any further — the
ReSpeaker's onboard class-D amp is rated **1 W**, full stop. Meanwhile the
BOM has specified a **3 W** speaker the whole time. The amp was starving the
speaker to a third of what it's rated for; no amount of software gain fixes
that.

**Building the button board anyway made the marginal cost of also carrying
audio small.** The KiCad project already existed for the button/lamp half.
Adding an amp IC and two mic connectors to it is a much smaller step than
starting a board from nothing.

### Why digital I2S, not a bigger analog amp bolted onto the same codec

The obvious minimal fix is to keep the ReSpeaker's TLV320AIC3104 codec for
mic capture (it works, and its driver overlay is already vendored in this
repo) and just tap its line/headphone output into a separate, more powerful
external Class-D amp. That is a perfectly valid path if you'd rather keep the
existing driver story unchanged.

This design goes further and drops the analog codec entirely, in favour of:

- **[MAX98357A](https://www.digikey.com/en/products/detail/analog-devices-inc-maxim-integrated/MAX98357AETE-T/4936122)** —
  I2S-in, Class-D amplifier **and** DAC in one chip. 3.2 W into 4 Ω at 5 V
  (10% THD). No analog output stage to design.
- **2x [ICS-43434](https://www.mouser.com/ProductDetail/Adafruit/6049)**
  I2S digital MEMS microphones (as the Adafruit breakout module, not a bare
  die on this board — see the note below on why). 65 dBA SNR, ±1 dB
  sensitivity matching between units, so a stereo pair needs no per-unit
  calibration.

Reasoning:

1. **It fixes the real bottleneck properly.** 3.2 W into a 3 W-rated speaker
   is close to the speaker's actual limit rather than a third of it — the
   mismatch that caused the complaint in the first place is gone, without
   over-driving a small speaker or needing a supply rail above the Pi's
   native 5 V.
2. **Fewer analog failure modes.** No mic-bias network, no PLL/MCLK
   configuration, no capture-gain tuning script fighting clipping headroom
   (`CAPTURE_GAIN_FRACTION` in `tools/set-audio-levels.sh` goes away
   entirely — the ICS-43434 is factory-trimmed and reports a fixed,
   documented sensitivity).
3. **It was the path I could verify.** Silicon-vendor datasheet hosts
   (Analog Devices, Mouser, TI) were unreachable from this session's network
   egress, so every pin assignment in the schematic below is cross-checked
   against an independently published, working KiCad symbol
   ([source](https://github.com/IMMRMKW/KICAD/blob/master/Max98357I2SMonoAmp.kicad_sym))
   rather than typed from memory against a chip I couldn't look up. I was
   **not** able to independently verify the ICS-43434's exact LGA pad
   numbering the same way, which is exactly why the mic is specified as a
   **pre-built, pre-verified breakout module** wired in over a header,
   rather than a bare 3.5 x 2.65 mm part with a footprint I'd be guessing
   the pad layout for. Fabricating a wrong LGA footprint from an unverified
   guess is the kind of mistake that only shows up after the boards come
   back, so this sidesteps it rather than risking it.

### The one thing this trades away: a devicetree overlay to write

The Pi's I2S peripheral is one hardware block. The ReSpeaker's codec did
playback and capture through one chip, on one I2S bus, and its overlay just
works. Running a separate playback-only chip (MAX98357A) and capture-only
mics (the ICS-43434s) simultaneously on that same bus needs a
`simple-audio-card`-style devicetree overlay with two DAI links — one
playback link to a dummy "codec" that's really just the amp, one capture
link the same way for the mics. This isn't invented for this project:
HiFiBerry's DAC+ADC Pro does exactly this pairing (separate DAC and ADC
chips presented as one card), and their overlay source is public to adapt.
It's new work, not a drop-in `dtoverlay=`, and it's a software task to do
**after** the board exists — it doesn't block ordering or assembling it.

## Choosing a board

The messaging stack sets a hard floor here. signal-cli is a Java application,
and the Signal protocol implementation it depends on (libsignal) is not
something that can be reimplemented casually. That means the board must run
Linux **and** a modern JVM — signal-cli 0.14 requires **JRE 25 or newer**,
which is ahead of what Debian stable ships, so `install.sh` fetches one rather
than relying on the distribution.

| Board | £ | Verdict |
|-------|--:|---------|
| **Pi Zero 2 W** | ~15 | ✅ **Cheapest that works.** Quad-core ARMv8, WiFi built in. 512 MB RAM is tight for a JVM — see below |
| Pi 4 / Pi 5 (2 GB+) | 45+ | ✅ Comfortable. What the default config assumes |
| Pi 3A+ | ~23 | ✅ Works. 512 MB, same RAM caveat as the Zero 2 W |
| **Pi Zero v1.3** | ~5 | ❌ **No.** Two independent blockers |
| **Pico 2 W** | ~7 | ❌ **No.** Not a Linux computer at all |

### Why not the Pi Zero v1.3

Two separate problems, either one fatal:

1. **It has no WiFi.** The v1.3 is the pre-wireless Zero — WiFi arrived with
   the Zero **W** in 2017. You would need a USB dongle plus an OTG adapter,
   which puts you back at Zero 2 W money in a bulkier package.
2. **ARMv6 cannot run signal-cli.** OpenJDK's Server VM requires ARMv7 with
   hardware floating point; on a Pi 1 or original Zero it refuses to start
   with *"Server VM is only supported on ARMv7+ VFP"*. signal-cli's maintainer
   has confirmed this is a JVM limitation with no practical workaround.

### Why not the Pico 2 W

The Pico 2 W is a **microcontroller**, not a small computer. It has 520 KB of
SRAM and 4 MB of flash, and runs MicroPython or bare C — there is no Linux, no
JVM, no filesystem worth the name. For this project that rules out signal-cli,
the Flask web UI, Opus encoding, TLS, and storing voice messages (4 MB of
flash holds roughly one minute of audio in total).

It would be a fine **I/O co-processor** — scanning buttons and driving lamps
over serial while a Pi does the real work — but that is strictly more parts and
more complexity than two £3 MCP23017s, for no benefit.

### Making 512 MB work (Zero 2 W / Pi 3A+)

The JVM is the memory hog. Three changes make it comfortable:

```bash
# Give the GPU the bare minimum
echo 'gpu_mem=16' | sudo tee -a /boot/firmware/config.txt

# Cap signal-cli's heap - add to /etc/little-voicemail/signal.env
echo 'JAVA_OPTS=-Xmx192m -XX:+UseSerialGC' | sudo tee -a /etc/little-voicemail/signal.env

# Compressed RAM swap, much kinder to the SD card than a swapfile
sudo apt install -y zram-tools
echo 'ALGO=zstd\nPERCENT=60' | sudo tee -a /etc/default/zramswap
sudo systemctl restart zramswap
```

Use a Pi Zero 2 W rather than a Pi 4 only if the cost matters to you; expect
sends to take a second or two longer while Opus encoding runs on the slower
core. There is also an experimental GraalVM native build of signal-cli that
drops the JVM entirely, but ARM64 native-image builds are slow and finicky to
produce, so it is not the recommended path.

## Audio design

### Signal chain

```
Pi I2S TX (GPIO21) ──▶ MAX98357A ──▶ speaker (3.2 W @ 4 Ω)
Pi I2S RX (GPIO20) ◀── ICS-43434 x2 (TDM, shared BCLK/WS/SD)
Pi I2C1 (GPIO2/3)  ◀▶ MCP23017 (buttons/lamps, address 0x20)
```

Playback and capture are two independent, unidirectional I2S links that
happen to share the Pi's BCLK and LRCLK lines (both chips are I2S slaves,
clocked by the Pi) but use separate data pins — DOUT (GPIO21) feeds the amp,
DIN (GPIO20) reads the mics. Neither the MAX98357A nor the ICS-43434 needs an
external MCLK; both derive their internal timing from BCLK/LRCLK, which is
one of the reasons this pairing needs so few supporting parts.

### MAX98357A (U2) — amplifier

| Pin | Name | Connects to |
|----:|------|-------------|
| 1 | DIN | Pi GPIO21 (I2S DOUT) |
| 2 | GAIN | Unconnected — default 9 dB. Strap to GND for 12 dB, or GND via 100 kΩ for 15 dB, if 9 dB proves too quiet once the speaker below is on it |
| 3, 11, 15 | GND | GND |
| 4 | SD_MODE | +3V3 (always enabled, left-channel output) |
| 7, 8 | VDD | +5V |
| 9 | OUTP | Speaker + |
| 10 | OUTN | Speaker − |
| 14 | LRCLK | Pi GPIO19 |
| 16 | BCLK | Pi GPIO18 |
| 5, 6, 12, 13 | N.C. | No connect |
| 17 | Thermal pad | GND |

Decoupling: 100 nF ceramic close to pins 7/8, plus a 10 µF bulk capacitor on
the same rail — standard practice for a Class-D amp's supply pins, damps the
switching-current transients the linear regulator alone won't.

A future hardware-mute feature is easy to add later without a respin: SD_MODE
could instead be driven from the MCP23017's spare **GPB7** pin (documented as
unused in `hardware/README.md`) instead of tied straight to 3V3, giving the
firmware a real hardware mute alongside the lamps it already drives. Not done
in this revision, to keep the first board's bring-up simple — noted here so
it isn't forgotten.

### ICS-43434 x2 (mic breakouts) — microphones

Wired as **Adafruit's I2S MEMS Microphone Breakout ([#6049](https://www.mouser.com/ProductDetail/Adafruit/6049))**,
not a bare part on this board (see [above](#why-digital-i2s-not-a-bigger-analog-amp-bolted-onto-the-same-codec)
for why). Each breakout carries its own decoupling; only these six signals
per unit reach this board via a 1x6 2.54 mm pin header:

| Breakout pin | Left mic (J9) | Right mic (J10) |
|--------------|----------------|-------------------|
| 3V / VIN | +3V3 | +3V3 |
| GND | GND | GND |
| SCK | Pi GPIO18 (shared) | Pi GPIO18 (shared) |
| WS | Pi GPIO19 (shared) | Pi GPIO19 (shared) |
| SD | Pi GPIO20 (shared) | Pi GPIO20 (shared) |
| SEL (L/R) | GND (left slot) | +3V3 (right slot) |

Both mics share one data line (Pi GPIO20) via I2S's time-division stereo
slots — tying one breakout's SEL low and the other's high is what makes them
answer in different slots on the same wire, exactly the trick the
[ICS-43434 reference design](https://quickboards.org/documentation/ics-43434-i2s-microphone-reference-design/)
uses for a stereo pair. Far-field pickup gets a small win from this too: two
independently-placed mics feeding two channels is more directional
information than the same two mics summed to mono would give the software.

### Power budget

| Load | Peak | Typical |
|------|-----:|--------:|
| MAX98357A into 4 Ω speaker | 3.2 W (~640 mA @ 5V, Class-D so real draw is lower) | well under during speech, which isn't a continuous tone |
| 2x ICS-43434 | ~3 mA total | negligible |
| MCP23017 + 7 lamps | 95 mA | as before |
| Pi 4 itself | up to ~1.2 A | — |

All comfortably inside the official 3 A USB-C supply's headroom; nothing here
changes the "use the official supply" advice in [Power](#power) below.

## Bill of materials

| Qty | Part | Approx. £ | Source |
|----:|------|----------:|--------|
| 1 | Raspberry Pi 4 Model B, 2 GB (Pi Zero 2 W also works) | 45 | [The Pi Hut](https://thepihut.com/) |
| 1 | microSD card, 32 GB A1 | 6 | any reputable brand |
| 1 | Official Pi USB-C PSU, 3 A | 8 | [The Pi Hut](https://thepihut.com/) — do not skimp, the amp draws real current |
| 1 | MCP23017-E/SP, DIP-28 | 3 | [Digi-Key](https://www.digikey.in/en/products/detail/microchip-technology/MCP23017-E-SP/MCP23017-E-SP-ND/894272) / [Mouser](https://www.mouser.com/ProductDetail/Microchip-Technology/MCP23017-E-SP) |
| 1 | **MAX98357AETE+T**, TQFN-16 3x3mm, I2S Class-D amp | 3 | [Digi-Key MAX98357AETE-T](https://www.digikey.com/en/products/detail/analog-devices-inc-maxim-integrated/MAX98357AETE-T/4936122) |
| 2 | **Adafruit I2S MEMS Microphone Breakout — ICS-43434** (#6049) | 6 each | [Mouser #6049](https://www.mouser.com/ProductDetail/Adafruit/6049) / [The Pi Hut](https://thepihut.com/products/adafruit-i2s-mems-microphone-breakout-ics-43434) |
| 1 | **RS PRO Miniature Speaker, 4 Ω, 3 W, 40 mm dia.** | 3 | [RS 0102760](https://uk.rs-online.com/web/p/miniature-speakers/0102760) — SPL ≥85 dB, 0 Hz–20 kHz |
| 6 | 30 mm illuminated button, **bare LED** | 12 | Arcade World UK, Pimoroni, The Pi Hut, or AliExpress in bulk |
| 1 | 60 mm illuminated button, **bare LED** | 5 | as above — the push-to-talk button, make it obviously the big one |
| 7 | 220 Ω resistor, 0.25 W | 1 | any distributor — sets lamp current, see [Lamps](hardware/README.md#resistor-sizing) |
| 1 | 100 nF ceramic capacitor | — | MCP23017 decoupling |
| 1 | 100 nF ceramic capacitor | — | MAX98357A supply decoupling, close to VDD pins |
| 1 | 10 µF ceramic/tantalum capacitor | — | MAX98357A supply bulk decoupling |
| 1 | PCB (this design) | ~10 for a small-batch run | schematic is in `hardware/little-voicemail.kicad_sch`; layout still needs doing in KiCad, see `hardware/README.md` |
| 1 | 40-pin GPIO stacking header | 3 | The Pi Hut / Rapid |
| 7 | 4-way JST-XH connector + crimps | 4 | one per button: switch pair + lamp pair |
| 2 | 1x6 2.54 mm pin header (socket) | 1 | one per mic breakout |
| — | Hook-up wire, 2.8 mm spade connectors | 5 | if your buttons take spades rather than solder lugs |
| 1 | Enclosure | 10–25 | laser-cut ply or a project box; see [Enclosure](#enclosure) |

**Total: roughly £115 for the Pi 4 build, £85 with a Pi Zero 2 W** — about the
same as the ReSpeaker-based BOM, since the amp/mic/expander parts cost is
similar; the difference is a PCB order instead of buying a pre-made HAT, and
one fewer board to stack.

### Substitutions worth knowing about

- **Keep the ReSpeaker HAT instead.** If you'd rather not do a PCB order at
  all, the original stacked design (ReSpeaker 2-Mics Pi HAT v2 + this board's
  earlier button-only revision) still works — see the amp-power caveat this
  document opens with. `git log` before this revision has that BOM.
- **PAM8302A instead of MAX98357A**, if you'd rather keep an analog signal
  path (e.g. you're reusing a codec board that already outputs
  line/headphone level audio). Mono, 2.5 W into 4 Ω, SO8, one gain-set
  resistor — simpler IC, but needs an analog source, so it doesn't remove
  the ICS-43434's win on the capture side.
- **SPH0645LM4H-B** is the commonly-cited drop-in successor once ICS-43434
  supply gets tight — same I2S/L-R-select interface, same breakout footprint
  family. Worth checking availability before ordering.
- **16 mm illuminated pushbuttons** (~£1.50 each) — electrically ideal: bare
  LED, you pick the current, and they are cheap. The objection is ergonomic.
  A 16 mm button has a ~12 mm cap, which is a fingertip-sized target for a
  four-year-old and leaves a 45 mm grid mostly empty panel. If you want
  smaller than 30 mm, 24 mm is the sensible floor — and keep the 60 mm
  push-to-talk whatever you do, since the design leans on it being
  unmistakable by feel.
- **Pre-wired 5 V LED buttons** — most 30 mm arcade buttons ship with an LED
  module that has its resistor built in and draws ~20 mA fixed. Seven of those
  is 140 mA, which still fits the expander's 150 mA package limit but leaves
  almost no margin. They work; you just lose the ability to tune brightness,
  and the schematic's series resistors become links. Bare-LED buttons are the
  better buy here.

## Wiring

### I²C address

| Chip | A2 A1 A0 | Address | Purpose |
|------|----------|---------|---------|
| MCP23017 | GND GND GND | `0x20` | Port A: seven switches. Port B: seven lamps |

The MCP23017 is now the **only** device on the I2C bus — the audio chain
below is entirely I2S, so there's no codec sharing SDA/SCL the way the
ReSpeaker HAT's did. Tie `RESET` (pin 18) to 3V3 — leaving it floating causes
intermittent resets that look like phantom button presses. Tie `A0`, `A1` and
`A2` (pins 15, 16, 17) to GND for address `0x20`.

Do **not** add I²C pull-up resistors. The Pi already fits 1.8 kΩ pull-ups on
SDA and SCL; another pair in parallel is unnecessary and pulls the bus harder
than it needs.

Check with `i2cdetect -y 1` — you should see `20` and nothing else (no codec
address to share the bus with any more).

### Buttons → MCP23017 port A

Every button switch goes between its expander pin and **GND**. The internal
pull-ups are enabled in software, so no external resistors are needed, and a
pressed button reads 0.

| Button | Expander pin | Chip pin |
|--------|--------------|---------:|
| Contact 1 | GPA0 | 21 |
| Contact 2 | GPA1 | 22 |
| Contact 3 | GPA2 | 23 |
| Contact 4 | GPA3 | 24 |
| Contact 5 | GPA4 | 25 |
| Contact 6 | GPA5 | 26 |
| **Push to talk** | GPA6 | 27 |

GPA7 (pin 28) is unused and left as an input.

### Lamps → MCP23017 port B

The lamps **sink** to the expander: anode to +5 V through a series resistor,
cathode to the pin. A pin driven **low** lights its lamp, and the pins idle
high. `leds.py` inverts in one place (`_write`), so everything above it reads
in positive logic.

```
  +5V ──[220 Ω]──▶|── MCP23017 GPB(n)      (drive LOW to light)
                  LED
```

| Lamp | Expander pin | Chip pin |
|------|--------------|---------:|
| Contact 1 | GPB0 | 1 |
| Contact 2 | GPB1 | 2 |
| Contact 3 | GPB2 | 3 |
| Contact 4 | GPB3 | 4 |
| Contact 5 | GPB4 | 5 |
| Contact 6 | GPB5 | 6 |
| **Push to talk** | GPB6 | 7 |

GPB7 (pin 8) is unused — see the [hardware-mute idea](#max98357a-u2--amplifier)
above for a candidate future use.

**Why sink rather than source.** The expander runs at 3.3 V and its output
high sags under load, leaving nothing for a white or blue LED at ~3.0 V
forward. Pulling the cathode down against a 5 V rail works for every colour.
When the pin is high there is only 1.7 V across resistor and LED, below the
forward voltage of any of them, so the lamp is properly off.

**Sizing the resistor.** 220 Ω from 5 V gives roughly:

| LED colour | Vf | Current | Seven lamps |
|------------|---:|--------:|------------:|
| White / blue | ~3.0 V | 9 mA | 64 mA |
| Green / yellow | ~2.2 V | 13 mA | 91 mA |
| Red | ~2.0 V | 14 mA | 95 mA |

All well inside the expander's 25 mA per pin and 150 mA per package. Go up to
470 Ω if you want them dimmer for a bedroom; 220 Ω is the brightest value that
is safe for every colour.

> **Check before you solder.** Some arcade buttons ship with 12 V LED modules
> that look identical to 5 V ones. On 5 V they glow dimly or not at all. The
> LED module usually unscrews and can be swapped for a bare LED.

### I2S → MAX98357A and mics

| Signal | Pi header pin | Connects to |
|--------|---------------|-------------|
| BCLK | GPIO18 (physical pin 12) | MAX98357A pin 16, both mic breakouts' SCK |
| LRCLK | GPIO19 (physical pin 35) | MAX98357A pin 14, both mic breakouts' WS |
| I2S DOUT (Pi transmits) | GPIO21 (physical pin 40) | MAX98357A pin 1 (DIN) |
| I2S DIN (Pi receives) | GPIO20 (physical pin 38) | both mic breakouts' SD, tied together |

### Speaker

Driven directly by the MAX98357A's OUTP/OUTN — a filterless Class-D output,
no external LC filter required for this application. Solder to a 2-pin
JST-PH 2.0 or bare leads into the RS PRO 40 mm speaker above. It gives up to
3.2 W into 4 Ω — noticeably louder than the 1 W the previous ReSpeaker-based
design could deliver into the same speaker, because the amp is finally sized
to what the speaker was always rated for.

### Schematic

`hardware/little-voicemail.kicad_sch` has the audio chain and the button/lamp
board drawn up on one sheet — every symbol, pin, and net. There is no PCB
layout file yet: see [hardware/README.md](hardware/README.md) for the net
list, board notes, the mechanical spec to set up in KiCad (board outline,
mounting holes, GPIO header position), and why the layout itself was left for
KiCad rather than hand-authored here.

## Power

The amp dominates here, same as before: seven lamps at 220 Ω add at most
~95 mA, but the Class-D amp draws real current on peaks. Use the official
3 A supply. If lamps dim when several are lit at once, that is brownout, not
a software bug — check the supply first.

The lamps run off the header's **+5 V**, not 3V3, so they do not load the
Pi's 3.3 V regulator. The MCP23017 and both mic breakouts sit on 3V3, at well
under a milliamp combined; the amp is the only new load of consequence on 5V.

## Enclosure

The six buttons want to be in a 3×2 grid at roughly 45 mm centres, with the
push-to-talk button clearly separated below and physically bigger so it is
unmistakable by feel. Six in two rows of three suits a small child better
than nine did: the same panel area gives more room around each target, and
there is less to scan.

Leave mic openings clear over both ICS-43434 breakouts — place them near
opposite edges of the enclosure, the same far-field logic as the old
ReSpeaker's two-mic placement. Drill 3–4 mm holes directly over each mic's
port.

Angle the top face back about 15°, so a child looking down at it sees the
labels straight on.

Print or write names next to each button — young children navigate by
position and picture far better than by reading, so consider a photo of each
person beside their button.
