# Setup

Flash a card, plug it in, open a web page. Twenty minutes, most of it waiting
for the card to write.

Wiring is in [HARDWARE.md](HARDWARE.md). This document covers software.

There is no SSH step. If you would rather install onto a Pi you already have,
that still works — see [Appendix A](#appendix-a--install-onto-an-existing-pi).

---

## 1. Flash the image

Download the latest `little-voicemail-<version>-arm64.img.xz` from
[Releases](https://github.com/guinius/Little_Voicemail/releases), and check it
against the `.sha256` next to it:

```bash
sha256sum -c little-voicemail-0.2.0-2026-08-03-arm64.img.xz.sha256
```

It is Raspberry Pi OS Lite 64-bit with everything already installed: the
ReSpeaker driver enabled, I²C on, signal-cli in place, the web UI set to start
on boot.

Write it with **Raspberry Pi Imager** → *Choose OS* → **Use custom**.

Then open Imager's settings (the gear, or *Edit Settings* when it offers) and
set:

- **Hostname**: `littlevoicemail`
- **WiFi**: your network name and password, and the right country

Imager's customisation works on this image exactly as it does on the official
one. Setting the WiFi here is the smooth path — but if you skip it, or mistype
it, the box has a way out. See step 2.

SSH is optional. You will not need it.

## 2. First boot

Put the card in, connect the ReSpeaker HAT, power it up. Give it two or three
minutes — the first boot expands the filesystem and reboots itself once.

Then open:

**https://littlevoicemail.local:8443**

The certificate is self-signed and generated on the device, so your browser
will warn once. That is expected — accept it. The connection is encrypted from
then on, and the private key never leaves the Pi.

### If that page does not load

The box could not join your WiFi. Rather than being unreachable, it puts up its
own network:

1. On your phone, join the WiFi network **`Little Voicemail setup`**.
   The password is `voicemail`.
2. Your phone should show a "Sign in to network" prompt. Tap it. If nothing
   appears, open **http://10.42.0.1** in a browser.
3. Pick your WiFi, type the password, and press **Join this network**.

The setup network then disappears — which is how you know it worked. Put your
phone back on your normal WiFi and open
**https://littlevoicemail.local:8443**.

If the password was wrong, the setup network comes back after about a minute so
you can try again. It is not possible to lock yourself out this way.

> If `littlevoicemail.local` does not resolve — some Android versions and some
> routers do not do mDNS — use the Pi's IP address instead. Your router's
> device list will show it.

## 3. Set the parent password

Set a password. This is the only thing standing between a curious child and the
settings, so make it something they will not guess.

## 4. Link a Signal account

You land on the **Signal** tab straight after setting the password.

> **Use a number the child is not otherwise using.** Linking makes the Pi a
> *companion device* of an existing Signal account, exactly like Signal
> Desktop. Anything sent to that account arrives here. A cheap PAYG SIM, or a
> free number from a service that can receive SMS, keeps the child's box
> separate from your own account.

Register that number in the Signal app on a phone first. Then press **Start
linking**.

**On Android** — tap **Open Signal and link this device**. Signal opens and asks
whether to link. Say yes. You can do this on the same phone you are reading the
page on; there is no QR code and no second device.

**On iPhone** — Apple's Signal app will not accept a link from outside the app.
Tapping the button sends you to Settings → Linked devices, and from there only a
camera scan works, which a phone cannot do to its own screen. So:

- Open **https://littlevoicemail.local:8443** on a second screen — a laptop,
  a tablet, another phone — and press **Start linking** there.
- Expand **Show the QR code**.
- On the iPhone: **Settings → Linked devices → Link New Device**, and scan it.

This is a restriction in Signal for iOS, not something the box can work around.

Either way, once the phone approves, the number is saved and the phone service
starts on its own. It takes half a minute to connect.

## 5. Add contacts

**Contacts** in the web UI. For each button, enter a nickname and the person's
number in full international form — `+447700900123`, not `07700900123`. The
person must already use Signal on that number.

"Get nicknames from Signal" fills in names from the linked account's contact
list, so you do not have to type them twice.

## 6. Quiet times

**Quiet times** gives you three independent windows:

- **School** — weekdays 09:00–15:15
- **Nap** — a short afternoon window
- **Bedtime** — 19:00–07:00, wrapping past midnight

Each has its own on/off switch and its own days. While one is running the box is
asleep: no ringtone, no lights, and any button press just flashes all nine lamps
three times. Messages still arrive and queue up quietly, and appear on the
buttons the moment the window ends.

## 7. Try it

1. Press button 1. It lights steady.
2. Hold push-to-talk. The big lamp lights. Say something.
3. Let go. Both lamps go out; the voice note arrives on the other phone.
4. Reply from that phone. The ringtone plays and button 1 starts flashing.
5. Press button 1. It plays the reply, then stays lit for 30 seconds so the
   child can answer straight back.

## 8. Sound levels (only if it needs it)

The image sets sensible mixer levels on first boot. If recordings come out too
quiet or playback is too loud, and you do have SSH enabled: `alsamixer`, F6 to
pick the ReSpeaker card, adjust, then `sudo alsactl store` to keep it across
reboots.

---

## Updating

The web UI checks GitHub whenever it loads. If there is a newer version,
**System** shows the running and available versions and an **Update and
restart** button. It pulls, reinstalls dependencies and reboots. A failed update
rolls the checkout back to where it started.

Updating this way does not touch your password, contacts or Signal link.

---

## Troubleshooting

**The web page does not load at all**
See "If that page does not load" in step 2 — the box will be advertising its own
setup network.

**Buttons and lights show "simulated"**
I²C is not working. `i2cdetect -y 1` should show `20`. If it shows nothing,
check `dtparam=i2c_arm=on` in `/boot/firmware/config.txt` and reboot. If it
shows some other address, check A0/A1/A2 (pins 15-17) are all tied to GND.

**No sound card**
`aplay -l` should list `seeed2micvoicec`. The image enables
`dtoverlay=respeaker-2mic-v2_0` already, so if the card is missing, the HAT is
usually not seated properly — power off before reseating it.

If you have the older **v1.0** HAT, that is a different codec (WM8960 rather
than the v2.0's TLV320AIC3104) and it needs a different overlay. Swap the line
in `/boot/firmware/config.txt` for `dtoverlay=wm8960-soundcard`, which ships
with Raspberry Pi OS, and reboot.

Ignore any guide — including older versions of this one — telling you to use
`dtoverlay=seeed-2mic-voicecard`. That belongs to Seeed's out-of-tree driver,
which broke after kernel 5.10 and which Seeed no longer recommend; the overlay
does not exist on a stock Raspberry Pi OS, so the line silently does nothing.

**Linking keeps failing**
Usually the clock. A box that has been unplugged for a long time can be far
enough out that Signal rejects it; it fixes itself a minute or so after the
network comes up. Try again then.

**Phantom presses, or lamps flickering on their own**
The `RESET` pin (18) on the MCP23017 is floating. Tie it to 3V3.

**Lamps dim when several are lit**
Power. Use the official 3 A supply. The lamps run from the header's +5 V, so
check that rail rather than 3V3.

**Lamps are all on when they should be off, or vice versa**
The lamps are active low — anode to +5 V through the resistor, cathode to the
expander pin. Wiring them the other way round inverts the whole panel.

**Voice notes arrive as file attachments**
You are on a signal-cli older than 0.14.2, before `--voice-note` existed.
`signal-cli --version` to check.

**`signal-cli --version` fails, or linking never starts**
signal-cli is Java wrapped around a Rust library loaded through JNI, and the
official release bundles that library for **x86_64 Linux only** — on a
Raspberry Pi there is no native library in the jar at all, so it cannot start.

`install.sh` handles this: it fetches a matching build from
[exquo/signal-libs-build](https://github.com/exquo/signal-libs-build) and
splices it into `libsignal-client-*.jar`, following
[signal-cli's own instructions](https://github.com/AsamK/signal-cli/wiki/Provide-native-lib-for-libsignal).
If that download failed — no network at install time, say — re-run the
installer, or pass a tarball you fetched yourself:

```bash
sudo LV_LIBSIGNAL_TARBALL=/path/to/libsignal_jni.so-v0.99.1-aarch64-unknown-linux-gnu.tar.gz \
    ./install.sh
```

The version must match the `libsignal-client-<version>.jar` in
`/opt/signal-cli-*/lib/`.

**Nothing happens when a message arrives**
Check a quiet time is not running — the Status page says so at the top.

**The lights stay on after reading on a phone**
Read receipts have to be enabled on the account: Signal → Settings → Privacy →
**Read receipts**. Without them, Signal never tells the Pi the message was read.
You can always clear a queue from the web UI instead.

**Locked out of the web UI**
This one does need SSH:
```bash
sudo -u voicemail /opt/little-voicemail/.venv/bin/python -c "
import sys; sys.path.insert(0, '/opt/little-voicemail')
from src.config import Config
Config('/etc/little-voicemail/config.json').set('', 'web', 'password_hash')"
```
Then reload the page to set a new one.

---

## Appendix A — install onto an existing Pi

If you already have Raspberry Pi OS Lite (64-bit, Bookworm or newer) running
and would rather not reflash:

```bash
curl -fsSL https://raw.githubusercontent.com/guinius/Little_Voicemail/master/install.sh | sudo bash
sudo reboot
```

The installer does everything the image does: packages, the ReSpeaker overlay,
I²C, signal-cli, the service user, the setup portal and the web UI. The reboot
is needed for the overlay and I²C to take effect.

Then pick up from step 2 above.

Check the hardware came up after the reboot:

```bash
aplay -l          # should list "seeed2micvoicec"
i2cdetect -y 1    # expect 20, alongside the codec's own address
```

If your board names its sound card something else, put the real name into
`/etc/little-voicemail/config.json`:

```json
"audio": {
  "input_device": "plughw:CARD=seeed2micvoicec,DEV=0",
  "output_device": "plughw:CARD=seeed2micvoicec,DEV=0"
}
```

Then `sudo systemctl restart little-voicemail`.

## Appendix B — link Signal over SSH

The web UI is the supported path. This is here for when it is not an option —
no browser to hand, or debugging.

```bash
sudo systemctl stop signal-cli
sudo -u voicemail signal-cli \
    --config /var/lib/little-voicemail/signal-cli \
    link -n "Little Voicemail"
```

It prints an `sgnl://linkdevice?uuid=...` URI and waits. Turn it into a QR code
— `qrencode -t ANSI "<paste the URI>"` prints one straight into the terminal
(`sudo apt install qrencode`) — and scan it from Signal on the phone:
**Settings → Linked devices → Link new device**. The command exits once linking
completes. Do not kill it before then.

Then tell the services which account to use:

```bash
sudo sed -i 's/^SIGNAL_ACCOUNT=.*/SIGNAL_ACCOUNT=+447700900123/' \
    /etc/little-voicemail/signal.env
sudo -u voicemail /opt/little-voicemail/.venv/bin/python -c "
import sys; sys.path.insert(0, '/opt/little-voicemail')
from src.config import Config
Config('/etc/little-voicemail/config.json').set('+447700900123', 'signal', 'account')"
sudo systemctl enable --now signal-cli little-voicemail
```

The number has to be in both places — `signal.env` is what the signal-cli
daemon reads, `config.json` is what the phone service and web UI read. Linking
from the web UI writes both for you.

Check it came up:

```bash
systemctl status signal-cli little-voicemail
journalctl -u little-voicemail -f
```

## Appendix C — build the image yourself

See [tools/image/README.md](tools/image/README.md).
