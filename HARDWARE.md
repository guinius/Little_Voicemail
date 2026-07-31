# Hardware

## Why this combination

The two constraints that drive the whole design:

**No handset.** A push-to-talk box for a small child wants far-field mics, so
they can talk at it from wherever they are standing rather than holding
something to their face. That rules out the USB handset from the Kids-Phone
project and points at a mic array.

**Twenty GPIO lines.** Nine illuminated buttons plus push-to-talk is ten
switches and ten lamps. The Pi has 26 usable GPIO pins, but the audio HAT
takes the I²S pins (18–21), both I²C pins, and a few more. There are not
twenty left. On top of that, ten arcade-button LEDs at ~20 mA each is 200 mA,
which exceeds the MCP23017's own 150 mA package limit — so the lamps need
driver arrays, not just expander pins.

Hence: audio on a HAT, buttons and lamps on I²C expanders behind ULN2803s.

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
  ULN2803 pin 9 (COM) ──► +5V
  ULN2803 pin 10 (GND) ──► GND
```

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
