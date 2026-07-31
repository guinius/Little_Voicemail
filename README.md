# Little Voicemail

A screenless push-to-talk voice messenger for kids. Nine big lit-up buttons,
one per person. Press a button, hold push-to-talk, say something — it arrives
as a voice message on the grown-up's phone. When a reply comes back, that
person's button glows until the child presses it and listens.

No screen, no feed, no typing, no way to reach anyone who is not on one of the
nine buttons.

```
        ┌─────────────────────────────┐
        │   ①      ②      ③          │
        │                             │
        │   ④      ⑤      ⑥          │     ← nine lit contact buttons
        │                             │
        │   ⑦      ⑧      ⑨          │
        │                             │
        │      ┌───────────────┐      │
        │      │  PUSH TO TALK │      │     ← hold to record
        │      └───────────────┘      │
        └─────────────────────────────┘
```

## Why Signal and not WhatsApp

This started as a WhatsApp project. WhatsApp cannot do it.

Meta's official WhatsApp Cloud API only lets a business send a free-form
message — including a voice note — within **24 hours of the other person
messaging first**. Outside that window you may only send pre-approved
templates, and **templates cannot contain audio**. A child pressing "Grandma"
on a Tuesday afternoon would simply fail unless Grandma happened to message
the device that morning. The unofficial libraries that get around this
(Baileys, whatsmeow) work by impersonating WhatsApp Web and carry a permanent,
unappealable ban risk on the number.

Signal has a documented, supported command-line client, sends real voice
notes, syncs read receipts across linked devices, and costs nothing. Everything
below is built on [signal-cli](https://github.com/AsamK/signal-cli).

## What it does

| | Feature |
|---|---|
| 1 | Nine buttons, one contact each |
| 2 | Parents assign contacts to buttons from the web UI |
| 3 | Selected contact lights steady for 30s, then lapses back to standby |
| 4 | Hold push-to-talk to record; auto-stops at 60s; the PTT lamp is lit while recording |
| 5 | The recording is sent as a real Signal voice note |
| 6 | Incoming messages flash that contact's button; you must listen before replying |
| 7 | A ringtone of your choosing plays on arrival |
| 8 | Parent web UI over HTTPS on the local network, password protected |
| 9 | Contacts set by phone number + nickname, or pulled from Signal |
| 10 | Ringtone selectable in the web UI |
| 11 | Three quiet-time windows (school, nap, bedtime), each independently toggleable |
| 12 | During quiet time the device sleeps; any press flashes all nine lights 3× |
| 13 | Multiple messages play back to back with a 1s gap |
| 14 | Reading a message on a parent's phone clears the light here automatically |
| 15 | The web UI checks GitHub for updates and can update and reboot in one click |

## Hardware

Roughly **£70–95** all in. See [HARDWARE.md](HARDWARE.md) for the full bill of
materials and part links.

- Raspberry Pi 4 (2 GB), or a **Pi Zero 2 W** for the cheapest build that works
  — signal-cli needs Linux and a modern JVM, which rules out the Pico 2 W and
  the ARMv6 Pi Zero v1.3 (see [HARDWARE.md](HARDWARE.md#choosing-a-board))
- ReSpeaker 2-Mics Pi HAT v2 — dual far-field mics, onboard amp
- 3 W 4 Ω speaker
- 9 × 30 mm illuminated arcade buttons + 1 × 60 mm for push-to-talk
- 2 × MCP23017 I²C expanders and 2 × ULN2803 driver arrays

The ReSpeaker mics are far-field, so there is **no handset** — the child talks
at the box from across the room.

## Install

On a fresh Raspberry Pi OS (Bookworm or newer):

```bash
curl -fsSL https://raw.githubusercontent.com/guinius/Little_Voicemail/master/install.sh | sudo bash
```

Then follow [SETUP.md](SETUP.md) to wire the buttons, link a Signal account and
assign contacts.

## How it is put together

```
src/
  main.py             entry point for the phone service
  app.py              the state machine — selection, recording, playback, quiet time
  config.py           the settings file the web UI writes and the phone reads
  quiet_hours.py      three windows, midnight-wrap aware
  messages.py         SQLite queue of unheard messages, survives power cuts
  audio.py            arecord → opus encode → send; ffplay for playback
  signal_client.py    signal-cli JSON-RPC client
  updater.py          GitHub version check and one-click self-update
  hardware/
    mcp23017.py       I²C expander driver
    buttons.py        debounce and hold detection
    leds.py           lamp patterns and the render loop
  web/
    app.py            Flask parent UI
    server.py         HTTPS with a self-signed certificate
```

Three systemd services: `signal-cli` (the messaging daemon),
`little-voicemail` (buttons, lights, audio) and `little-voicemail-web`
(the parent UI). They are deliberately separate — a crash in the web UI
cannot take the phone down.

## Development

It runs off-device with simulated hardware, so the UI and logic can be worked
on from a laptop:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest            # 83 tests
.venv/bin/python -m src.web.server --no-tls --port 8080
```

With no I²C bus present the hardware layer falls back to a null bus and logs
that buttons and lights are simulated.

## Licence

MIT
