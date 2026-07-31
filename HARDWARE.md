# Hardware

## Why this combination

The two constraints that drive the whole design:

**No handset.** A push-to-talk box for a small child wants far-field mics, so
they can talk at it from wherever they are standing rather than holding
something to their face. That rules out the USB handset from the Kids-Phone
project and points at a mic array.

**Twenty GPIO lines.** Nine illuminated buttons plus push-to-talk is ten
switches and ten lamps. Twenty lines is more than the Pi can spare once the
audio HAT has taken its share, and ten arcade-button LEDs at ~20 mA each is
200 mA, which no GPIO source on the board can supply. Both problems are
solved off-board.

Hence: audio on a HAT, buttons and lamps on I²C expanders behind ULN2803s.
The next section works through why, because "just use the Pi's own pins" is
the obvious question and the answer is not obvious.

## Why the MCP23017s stay

The expanders look like the easiest parts to design out. They are not, and
the reasoning is worth writing down so it does not get relitigated.

### They cost zero GPIO pins

This is the part that settles it. Both expanders hang off SDA/SCL — and the
ReSpeaker HAT **already occupies I²C** for codec control. That bus is spent
whether or not the expanders are on it.

So removing them frees nothing. It takes twenty lines that currently cost no
pin budget at all and moves them onto twenty pins you would otherwise still
have.

### The twenty pins do exist, but only just

For the record, because the naive count is misleading in the other direction:
the Pi has 26 usable GPIO (BCM 2–27; 0 and 1 are the HAT ID EEPROM). The
HAT's unavoidable claim is only six of them — I²C (2, 3) and I²S (18–21).
That leaves:

```
4 5 6 7 8 9 10 11 12 13 14 15 16 17 22 23 24 25 26 27
└────────────────── exactly 20 ──────────────────────┘
```

Exactly twenty, with nothing to spare. Taking them means giving up:

| Pins | What goes |
|------|-----------|
| 7–11 | **SPI0**, and with it the HAT's three onboard APA102 RGB LEDs |
| 14, 15 | **The UART serial console** — the recovery path on a headless box when the network is what is broken |
| 12, 13 | The Grove port, and the only two hardware-PWM pins, burned on on/off lamps |
| 17 | Conflicts with the HAT's own user button, which is physically wired to this pin |

Plus no margin whatsoever: not one spare pin for a status LED, a volume
knob, or anything thought of later. And GPIO 9–27 default to pull-down at
boot while 0–8 pull up, with pins reassigned to ALT functions partway
through startup — so lamps wired straight to GPIO flicker semi-randomly for
the twenty seconds the Pi takes to boot.

### The Pi cannot drive the lamps anyway

The decisive one. Raspberry Pi GPIO is rated **16 mA per pin and roughly
50 mA total across all pins**. The MCP23017's 150 mA package limit is three
times more generous than the Pi's whole-chip budget.

Ten lamps at even 9 mA is 90 mA — comfortable on an expander, well over the
limit direct from the Pi. Going direct to GPIO therefore still needs driver
arrays, so the ULN2803s come back and the only thing achieved is spending
every remaining pin.

### Summary

| Approach | Chips | Extra Pi pins | Lamp current | Spare GPIO |
|----------|------:|--------------:|--------------|-----------:|
| **MCP23017 + ULN2803** (this design) | 4 | **0** | 200 mA through the ULN2803s ✅ | ~12 |
| Direct to GPIO | 2 (drivers still needed) | **20** | 90 mA against a ~50 mA budget ❌ | **0** |

Smaller buttons do not change this. A 16 mm illuminated pushbutton is still
one switch and one lamp — two lines, exactly like a 30 mm arcade button. Body
diameter has nothing to do with the pin count. What smaller bare-LED buttons
*can* change is the current, since they ship without a built-in resistor and
let you choose it; see [Substitutions](#substitutions-worth-knowing-about).

## Choosing a board

The messaging stack sets a hard floor here. signal-cli is a Java application,
and the Signal protocol implementation it depends on (libsignal) is not
something that can be reimplemented casually. That means the board must run
Linux **and** a modern JVM.

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
| 9 | 30 mm illuminated arcade button, 5 V LED | 18 | Generic Sanwa-style. Get the 5 V versions, not 12 V |
| 1 | 60 mm illuminated arcade button, 5 V LED | 5 | The push-to-talk button — make it obviously the big one |
| 2 | MCP23017 I²C GPIO expander, DIP-28 | 6 | One for switches, one for lamps |
| 2 | ULN2803A Darlington array, DIP-18 | 3 | Sinks the LED current the expander cannot |
| 1 | Perfboard or small protoboard | 4 | |
| 1 | 40-pin GPIO stacking header | 3 | To reach the pins the HAT sits on |
| — | Hook-up wire, 2.8 mm spade connectors | 6 | Arcade buttons take spades |
| 1 | Enclosure | 10–25 | Laser-cut ply or a project box; see below |

**Total: roughly £130 for the Pi 4 build, or £95–100 with a Pi Zero 2 W.**

### Where to buy

- ReSpeaker HAT — [Seeed Studio](https://www.seeedstudio.com/ReSpeaker-2-Mics-Pi-HAT-v2.html), The Pi Hut, Pimoroni
- Arcade buttons — Arcade World UK, Pimoroni, or AliExpress in bulk (cheapest
  by a distance if you can wait). Search "30mm illuminated arcade button 5V"
- MCP23017 / ULN2803 — The Pi Hut, Rapid, Mouser

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
- **16 mm illuminated pushbuttons** (~£1.50 each) — these ship with a *bare*
  LED and no built-in resistor, so you pick the current with a series resistor
  (220 Ω–1 kΩ). At ~9 mA each, ten lamps draw 91 mA, which fits inside the
  MCP23017's 150 mA package limit — so **the ULN2803s could be dropped** and
  the lamps driven straight off expander #2.

  Two catches. Wire them as active-low sinks (anode → +5 V through the
  resistor, cathode → expander pin, drive **low** to light). Sourcing from the
  expander does not work: it runs at 3.3 V and sags to ~2.7 V under load,
  while white and blue LEDs need ~3.0 V. Sinking from the 5 V rail gives
  proper headroom for any colour. This also inverts the LED word, so
  `leds.py` and `configure_outputs()` would need to change with it.

  The bigger objection is ergonomic: a 16 mm button has a ~12 mm cap, which
  is a fingertip-sized target for a four-year-old and leaves the 45 mm grid
  mostly empty panel. If you want smaller than 30 mm, 24 mm is the sensible
  floor — and keep the 60 mm push-to-talk whatever you do, since the design
  leans on it being unmistakable by feel.

## Wiring

### I²C addresses

| Chip | A2 A1 A0 | Address | Purpose |
|------|----------|---------|---------|
| MCP23017 #1 | GND GND GND | `0x20` | Ten button inputs |
| MCP23017 #2 | GND GND 3V3 | `0x21` | Ten lamp outputs |

Both share SDA (GPIO 2) and SCL (GPIO 3), which the ReSpeaker HAT passes
through. Tie `RESET` (pin 18) on both chips to 3V3 — leaving it floating
causes intermittent resets that look like phantom button presses.

Check them with `i2cdetect -y 1` — you should see `20` and `21`.

### Buttons → MCP23017 #1 (0x20)

Every button switch goes between its expander pin and **GND**. The internal
pull-ups are enabled in software, so no external resistors are needed.

| Button | Expander pin | Chip pin |
|--------|--------------|---------:|
| Contact 1 | GPA0 | 21 |
| Contact 2 | GPA1 | 22 |
| Contact 3 | GPA2 | 23 |
| Contact 4 | GPA3 | 24 |
| Contact 5 | GPA4 | 25 |
| Contact 6 | GPA5 | 26 |
| Contact 7 | GPA6 | 27 |
| Contact 8 | GPA7 | 28 |
| Contact 9 | GPB0 | 1 |
| **Push to talk** | GPB1 | 2 |

### Lamps → MCP23017 #2 (0x21) → ULN2803

The expander drives the ULN2803 inputs; the ULN2803 outputs sink the LED
cathodes. LED anodes all go to **+5 V**.

```
  MCP23017 #2 pin ──► ULN2803 IN(n)      ULN2803 OUT(n) ──► LED cathode
                                                 LED anode ──► +5V
  ULN2803 pin 9  (GND) ──► GND
  ULN2803 pin 10 (COM) ──► +5V, or leave unconnected
```

> **Pin 9 is GND and pin 10 is COM**, not the other way round. Swapping them
> puts +5 V on the ground pin and ties the flyback-diode common to 0 V, which
> will not work and will most likely destroy the chip. COM only matters for
> inductive loads; with LEDs it can simply be left floating.

| Lamp | Expander pin | ULN2803 |
|------|--------------|---------|
| Contact 1–8 | GPA0–GPA7 | #1, channels 1–8 |
| Contact 9 | GPB0 | #2, channel 1 |
| Push to talk | GPB1 | #2, channel 2 |

Most 5 V arcade buttons have the resistor built in. If yours are bare LEDs,
put 150 Ω in series with each one.

> **Check before you solder.** Some arcade buttons ship with 12 V LED modules
> that look identical. On 5 V they glow dimly or not at all. The LED module
> usually unscrews and can be swapped.

### Speaker

Solder to the ReSpeaker's JST 2.0 speaker pads, or use the 3.5 mm jack into
a powered speaker if you prefer. The onboard amp gives 1 W into 8 Ω — loud
enough for a bedroom, not for a garden.

## Power

The amp and ten LEDs together can pull well over an amp on top of the Pi.
Use the official 3 A supply. If lamps flicker when several are lit at once,
that is brownout, not a software bug — check the supply first.

## Enclosure

The nine buttons want to be in a 3×3 grid at roughly 45 mm centres, with the
push-to-talk button clearly separated below and physically bigger so it is
unmistakable by feel.

Leave the mic openings clear — the ReSpeaker's two mics are at opposite edges
of the board, and burying them behind a panel ruins the far-field pickup.
Drill 3–4 mm holes directly over each one.

Angle the top face back about 15°, so a child looking down at it sees the
labels straight on.

Print or write names next to each button — young children navigate by
position and picture far better than by reading, so consider a photo of each
person beside their button.
