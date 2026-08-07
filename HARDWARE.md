# Hardware

## Why this combination

The two constraints that drive the whole design:

**No handset.** A push-to-talk box for a small child wants far-field mics, so
they can talk at it from wherever they are standing rather than holding
something to their face. That rules out the USB handset from the Kids-Phone
project and points at a mic array.

**Fourteen GPIO lines.** Six illuminated buttons plus push-to-talk is seven
switches and seven lamps. Fourteen lines is more than the Pi can spare
alongside the audio HAT once you want to keep a serial console, and seven
lamps draw more current than the Pi's GPIO is allowed to supply. Both
problems are solved by one expander.

Hence: audio on a HAT, buttons and lamps on a single I2C expander. The next
section works through why, because "just use the Pi's own pins" is the
obvious question and at seven buttons the answer is genuinely close.

## Why the MCP23017 stays

At ten buttons the expanders were unarguable — twenty lines simply did not
exist. At seven the pin count alone no longer settles it, so the reasoning is
worth writing down.

### It costs zero GPIO pins

This is the part that settles it. The expander hangs off SDA/SCL — and the
ReSpeaker HAT **already occupies I2C** for codec control. That bus is spent
whether or not the expander is on it.

So removing it frees nothing. It takes fourteen lines that currently cost no
pin budget at all and moves them onto fourteen pins you would otherwise still
have.

### The fourteen pins do exist

Unlike at ten buttons, they genuinely fit. The Pi has 26 usable GPIO
(BCM 2-27; 0 and 1 are the HAT ID EEPROM), and the HAT's unavoidable claim is
only six of them — I2C (2, 3) and I2S (18-21). That leaves exactly twenty:

```
4 5 6 7 8 9 10 11 12 13 14 15 16 17 22 23 24 25 26 27
```

Fourteen of twenty, with six spare. You would have to spend SPI0 (7-11),
which costs the HAT's three onboard RGB LEDs, but you could keep the UART
console on 14/15 and stay clear of GPIO 17, which the HAT's own user button
is wired to.

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

Note what dropping from ten buttons to seven *did* buy: the second expander
and both ULN2803 driver arrays are gone. Four chips became one.

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

## Bill of materials

| Qty | Part | Approx. £ | Notes |
|----:|------|----------:|-------|
| 1 | Raspberry Pi 4 Model B, 2 GB | 45 | Pi Zero 2 W (~£15) also works and is plenty |
| 1 | microSD card, 32 GB A1 | 6 | |
| 1 | Official Pi USB-C PSU, 3 A | 8 | Do not skimp — the amp draws real current |
| 1 | **ReSpeaker 2-Mics Pi HAT v2** | 12 | Dual far-field mics, TLV320AIC3104 codec, 1 W class-D amp, JST speaker out |
| 1 | 3 W 4 Ω speaker, 40–50 mm | 4 | JST-PH 2.0 or solder to the pads |
| 6 | 30 mm illuminated button, **bare LED** | 12 | Contact buttons. Bare-LED type, not a pre-wired 5 V module — see below |
| 1 | 60 mm illuminated button, **bare LED** | 5 | The push-to-talk button — make it obviously the big one |
| 1 | MCP23017 I²C GPIO expander, DIP-28 | 3 | Port A reads the switches, port B drives the lamps |
| 7 | 220 Ω resistor, 0.25 W | 1 | One per lamp. Sets LED current; see [Lamps](#lamps--mcp23017-port-b) |
| 1 | 100 nF ceramic capacitor | — | Decoupling, across the expander's VDD/VSS |
| 1 | PCB or perfboard | 4 | See `hardware/little-voicemail.kicad_sch` |
| 1 | 40-pin GPIO stacking header | 3 | To reach the pins the HAT sits on |
| 7 | 4-way JST-XH connector + crimps | 4 | One per button: switch pair + lamp pair |
| — | Hook-up wire, 2.8 mm spade connectors | 5 | If your buttons take spades rather than solder lugs |
| 1 | Enclosure | 10–25 | Laser-cut ply or a project box; see below |

**Total: roughly £115 for the Pi 4 build, or £85 with a Pi Zero 2 W.**

> **Get the v2.0 HAT, and mind the driver advice you find online.** The v2.0
> swapped the v1.0's WM8960 codec for the TLV320AIC3104 above, which is what
> buys it Pi 5 support and 8–96 kHz. Both codecs have mainline kernel drivers,
> so all either needs is a device-tree overlay: `respeaker-2mic-v2_0` for the
> v2.0 — vendored in `tools/image/` and compiled by `install.sh`, because it
> does not ship with Raspberry Pi OS — or the in-tree `wm8960-soundcard` for a
> v1.0, selected with `LV_AUDIO_OVERLAY=wm8960-soundcard`.
>
> Do **not** install Seeed's out-of-tree `seeed-voicecard` driver, and ignore
> any guide telling you to set `dtoverlay=seeed-2mic-voicecard`. That driver
> broke after kernel 5.10 and Seeed themselves have moved off it; the overlay
> does not exist on a stock Raspberry Pi OS, so the line does nothing at all
> and you get a Pi that boots with no sound card.

### Where to buy

- ReSpeaker HAT — [Seeed Studio](https://www.seeedstudio.com/ReSpeaker-2-Mics-Pi-HAT-v2.html), The Pi Hut, Pimoroni
- Buttons — Arcade World UK, Pimoroni, The Pi Hut, or AliExpress in bulk.
  Search "30mm illuminated arcade button", and check whether the LED is bare
  or a pre-wired 5 V module before ordering
- MCP23017, resistors, connectors — The Pi Hut, Rapid, Mouser

### Substitutions worth knowing about

- **ReSpeaker 4-Mic Array** (~£25) — better pickup and a ring of 12 RGB LEDs,
  but it uses more GPIO and has no onboard amplifier, so you would need a
  separate amp board. Not worth it here.
- **USB conference speakerphone** (Anker PowerConf, ~£60–90) — one USB plug,
  excellent echo-cancelled mic, leaves every GPIO free. Genuinely the
  easiest path if you do not mind the size and cost. Set `input_device` and
  `output_device` in the config to the USB card and skip the HAT entirely.
- **Cheap USB mic + powered speaker** (~£10) — works, but the pickup is poor
  enough that a child has to lean in, which defeats the point.
- **Pre-wired 5 V LED buttons** — most 30 mm arcade buttons ship with an LED
  module that has its resistor built in and draws ~20 mA fixed. Seven of those
  is 140 mA, which still fits the expander's 150 mA package limit but leaves
  almost no margin. They work; you just lose the ability to tune brightness,
  and the schematic's series resistors become links. Bare-LED buttons are the
  better buy here.
- **16 mm illuminated pushbuttons** (~£1.50 each) — electrically ideal: bare
  LED, you pick the current, and they are cheap. The objection is ergonomic.
  A 16 mm button has a ~12 mm cap, which is a fingertip-sized target for a
  four-year-old and leaves a 45 mm grid mostly empty panel. If you want
  smaller than 30 mm, 24 mm is the sensible floor — and keep the 60 mm
  push-to-talk whatever you do, since the design leans on it being
  unmistakable by feel.

## Wiring

### I²C address

| Chip | A2 A1 A0 | Address | Purpose |
|------|----------|---------|---------|
| MCP23017 | GND GND GND | `0x20` | Port A: seven switches. Port B: seven lamps |

It shares SDA (GPIO 2) and SCL (GPIO 3) with the codec, which the ReSpeaker
HAT passes through. Tie `RESET` (pin 18) to 3V3 — leaving it floating causes
intermittent resets that look like phantom button presses. Tie `A0`, `A1` and
`A2` (pins 15, 16, 17) to GND for address `0x20`.

Do **not** add I²C pull-up resistors. The Pi already fits 1.8 kΩ pull-ups on
SDA and SCL; another pair in parallel is unnecessary and pulls the bus harder
than it needs.

Check with `i2cdetect -y 1` — you should see `20`, alongside the codec's own
address.

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

GPB7 (pin 8) is unused.

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

### Schematic

`hardware/little-voicemail.kicad_sch` has the whole thing drawn up, ready to
turn into a PCB. See [hardware/README.md](hardware/README.md) for the net list
and board notes.

### Speaker

Solder to the ReSpeaker's JST 2.0 speaker pads, or use the 3.5 mm jack into
a powered speaker if you prefer. Both are driven by the same onboard amp,
already amplified — wire a raw speaker straight to the JST pads, no
external amp needed. It gives 1 W into 8 Ω — loud enough for a bedroom, not
for a garden.

**If it sounds much quieter than that,** check the codec's own volume
levels before suspecting the wiring. The TLV320AIC3x kernel driver's
defaults leave real headroom unused on every boot — `PCM`, `HP DAC` and
`Line DAC` playback volumes all come up around -23.5 dB below their own
maximum — and nothing touches them otherwise, so a fresh boot is quiet by
default rather than by fault. `install.sh` installs
`little-voicemail-audio-levels.service`, a one-shot unit that maxes every
`*Playback Volume` / unmutes every `*Playback Switch` control the card
exposes on every boot (see `tools/set-output-levels.sh` — it discovers the
controls by name rather than hardcoding TLV320AIC3104-specific ones, so it
also covers a v1.0/WM8960 board). To apply it immediately without
rebooting:

```bash
sudo /opt/little-voicemail/tools/set-output-levels.sh
```

## Testing without the MCP23017

This is a **temporary bench-test variant**, for exercising Signal linking,
audio, the web UI and everything else while the real MCP23017 is on order.
It is not a replacement for the design above — see
["Why the MCP23017 stays"](#why-the-mcp23017-stays) for why the expander is
the right permanent choice, in particular the current-budget table below,
which this variant deliberately sits at the edge of.

The `claude/rpi-direct-led-switch-test-pwjan8` branch wires the seven
switches and seven lamps straight onto the Pi header instead — no expander,
no I²C for this part of the circuit (the ReSpeaker HAT still needs I²C for
its own codec). `src/hardware/gpio_direct.py` is a drop-in replacement for
the MCP23017 driver at the software layer: `buttons.py` and `leds.py` are
completely unchanged, because both drivers expose the same four methods
(`configure_inputs`, `configure_outputs`, `read_gpio`, `write_gpio`).

### Checking for clashes with the ReSpeaker HAT first

Before picking pins, everything the HAT itself needs has to be ruled out,
not just I²C/I²S:

| HAT claim | Pins | Why it's a hard clash |
|-----------|------|------------------------|
| I²C | GPIO 2, 3 | Codec control — shared with the expander normally, still needed here |
| I²S | GPIO 18–21 | Audio to/from the codec |
| SPI0 | GPIO 7–11 | The HAT's **own onboard RGB LEDs** are wired here. This project never drives them, but a switch or lamp sharing the line with the HAT's own LED driver chip is a real electrical conflict, not just a software one |
| HAT user button | GPIO 17 | A physical button built into the ReSpeaker board itself is wired to this pin. Reusing it fights that button directly |
| ID EEPROM | GPIO 0, 1 | Read once at boot to identify the HAT |

That rules out 12 of the Pi's 26 usable pins (2, 3, 7–11, 17, 18–21 — 0 and 1
were never usable anyway), leaving fourteen: exactly the number needed for
seven switches and seven lamps, with nothing spare. Getting to fourteen
means giving up the two pins HARDWARE.md's main design deliberately keeps
free for a serial console — **GPIO 14/15 (UART) are used here for contact
buttons 2 and 3**. Disable the serial console before wiring this up
(`sudo raspi-config` → *Interface Options* → *Serial Port* → login shell
*No*), or a debounced button read is the least of your problems if
something is also trying to run a getty on those pins. SSH over WiFi still
works exactly as normal.

### Pin table

Switches — input, internal pull-up, switch to **GND** (identical wiring to
the expander's port A, just landing on Pi pins instead of GPA0–6):

| Button | BCM GPIO | Header pin |
|--------|---------:|-----------:|
| Contact 1 | GPIO4 | 7 |
| Contact 2 | GPIO14 | 8 |
| Contact 3 | GPIO15 | 10 |
| Contact 4 | GPIO27 | 13 |
| Contact 5 | GPIO22 | 15 |
| Contact 6 | GPIO23 | 16 |
| **Push to talk** | GPIO24 | 18 |

Lamps — output, active low, cathode to the pin, anode to **+5 V** through a
**470 Ω** resistor (up from the expander's 220 Ω — see the current budget
below for why):

| Lamp | BCM GPIO | Header pin |
|------|---------:|-----------:|
| Contact 1 | GPIO25 | 22 |
| Contact 2 | GPIO5 | 29 |
| Contact 3 | GPIO6 | 31 |
| Contact 4 | GPIO12 | 32 |
| Contact 5 | GPIO13 | 33 |
| Contact 6 | GPIO16 | 36 |
| **Push to talk** | GPIO26 | 37 |

Switches cluster on header pins 7–18, lamps on pins 22–37, so the two looms
land on opposite ends of the header rather than interleaved — easier to
keep straight with jumper wires on a breadboard.

### Current budget — this is the part that's tight

The Pi's GPIO is rated ~16 mA per pin and **~50 mA total across all pins at
once**, versus the MCP23017's 150 mA package limit. At 220 Ω the lamps would
draw 63–91 mA total, well past that — hence 470 Ω here instead:

| LED colour | Vf | Current at 470 Ω | Seven lamps |
|------------|---:|------------------:|------------:|
| White / blue | ~3.0 V | ~4 mA | ~30 mA |
| Green / yellow | ~2.2 V | ~6 mA | ~42 mA |
| Red | ~2.0 V | ~6.4 mA | ~45 mA |

Worst case is roughly 45 of the Pi's ~50 mA budget — nearly all of it, with
the lamps noticeably dimmer than the expander's 220 Ω gives them. That is
expected, not a fault: it is the tradeoff for losing the expander's separate
150 mA rail. Do not add anything else to spare GPIO pins on this variant,
and if lamps flicker or the Pi resets when several light at once, that is
the GPIO budget or the 5 V rail sagging, not a bug — check those before
suspecting the code.

Boot-time flicker is also expected here in a way it isn't with the
expander: GPIO 9–27 default to pull-down at power-on and pins get
reassigned to ALT functions partway through startup, so lamps wired
straight to Pi GPIO can flash semi-randomly for the ~20 seconds the Pi
takes to boot. The expander's outputs are high-Z until the driver
configures them; direct GPIO has no such guarantee.

### Reverting once the MCP23017 arrives

The only file that changed to make this variant work is
`src/hardware/__init__.py` (it builds a `gpio_direct.DirectGPIO` instead of
an `mcp23017.MCP23017`). `mcp23017.py` itself, its tests, and the schematic
in `hardware/` were left untouched, so switching back is checking out that
one file from `master` — or just merging this branch's changes back out.

## Power

The amp dominates here: seven lamps at 220 Ω add at most ~95 mA, but the
class-D amp draws real current on peaks. Use the official 3 A supply. If
lamps dim when several are lit at once, that is brownout, not a software
bug — check the supply first.

The lamps run off the header's **+5 V**, not 3V3, so they do not load the
Pi's 3.3 V regulator. Only the expander itself sits on 3V3, at under a
milliamp.

> Running the temporary direct-GPIO variant instead? See
> ["Testing without the MCP23017"](#testing-without-the-mcp23017) above —
> its lamps load the Pi's own GPIO budget rather than the expander's, and
> that budget is the tight constraint there, not the amp.

## Enclosure

The six buttons want to be in a 3×2 grid at roughly 45 mm centres, with the
push-to-talk button clearly separated below and physically bigger so it is
unmistakable by feel. Six in two rows of three suits a small child better
than nine did: the same panel area gives more room around each target, and
there is less to scan.

Leave the mic openings clear — the ReSpeaker's two mics are at opposite edges
of the board, and burying them behind a panel ruins the far-field pickup.
Drill 3–4 mm holes directly over each one.

Angle the top face back about 15°, so a child looking down at it sees the
labels straight on.

Print or write names next to each button — young children navigate by
position and picture far better than by reading, so consider a photo of each
person beside their button.
