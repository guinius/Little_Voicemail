# Setup

Start to finish, roughly an evening — most of it waiting for `apt`.

Wiring is in [HARDWARE.md](HARDWARE.md). This document covers software.

---

## 1. Prepare the Pi

Flash **Raspberry Pi OS Lite (64-bit, Bookworm or newer)** with Raspberry Pi
Imager. In the Imager's advanced settings, set the hostname to
`littlevoicemail`, enable SSH, and enter your WiFi details — the box has no
screen, so getting networking right up front saves a lot of pain.

Boot it and SSH in:

```bash
ssh pi@littlevoicemail.local
```

## 2. Enable the ReSpeaker HAT

On Bookworm the driver is a device-tree overlay:

```bash
echo 'dtoverlay=seeed-2mic-voicecard' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

After the reboot, confirm the card is there:

```bash
aplay -l     # should list "seeed2micvoicec"
arecord -l   # same card, for capture
```

Test it end to end — record five seconds and play it back:

```bash
arecord -D plughw:CARD=seeed2micvoicec,DEV=0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/t.wav
aplay -D plughw:CARD=seeed2micvoicec,DEV=0 /tmp/t.wav
```

If you hear yourself, the hard part is done. If the card is named differently
on your board, note the exact name — it goes in the config in step 6.

## 3. Run the installer

```bash
curl -fsSL https://raw.githubusercontent.com/guinius/Little_Voicemail/master/install.sh | sudo bash
```

It installs packages, enables I²C, fetches signal-cli, creates the `voicemail`
service user, and starts the web UI. It does **not** start the phone service
yet — there is no Signal account to talk to.

Verify the expander is visible:

```bash
i2cdetect -y 1     # expect 20, alongside the codec's own address
```

## 4. Set the parent password

Open **https://littlevoicemail.local:8443** (or the Pi's IP address).

The certificate is self-signed and generated on the device, so your browser
will warn once. That is expected — accept it. The connection is encrypted from
then on, and the private key never leaves the Pi.

Set a password. This is the only thing standing between a curious child and
the settings, so make it something they will not guess.

## 5. Link a Signal account

> **Use a number the child is not otherwise using.** Linking makes the Pi a
> *companion device* of an existing Signal account, exactly like Signal
> Desktop. Anything sent to that account arrives here. A cheap PAYG SIM, or a
> free number from a service that can receive SMS, keeps the child's device
> separate from your own account.

Register the number in the Signal app on a phone first. Then, on the Pi:

```bash
sudo -u voicemail signal-cli \
    --config /var/lib/little-voicemail/signal-cli \
    link -n "Little Voicemail"
```

It prints an `sgnl://linkdevice?uuid=...` URI. Turn it into a QR code —
`qrencode -t ANSI "<paste the URI>"` prints one straight into the terminal
(`sudo apt install qrencode`).

In Signal on the phone: **Settings → Linked devices → Link new device**, and
scan it. The command exits once linking completes.

Now tell the services which account to use:

```bash
sudo sed -i 's/^SIGNAL_ACCOUNT=.*/SIGNAL_ACCOUNT=+447700900123/' \
    /etc/little-voicemail/signal.env
sudo systemctl enable --now signal-cli little-voicemail
```

Put the same number into the web UI under **System**, or directly:

```bash
sudo -u voicemail python3 - <<'EOF'
from src.config import Config
c = Config("/etc/little-voicemail/config.json")
c.set("+447700900123", "signal", "account")
EOF
```

Check it came up:

```bash
systemctl status signal-cli little-voicemail
journalctl -u little-voicemail -f
```

The web UI's Status page should show Signal as **connected** and buttons and
lights as **detected**.

## 6. Audio device names (only if step 2 gave a different name)

Edit `/etc/little-voicemail/config.json`:

```json
"audio": {
  "input_device": "plughw:CARD=seeed2micvoicec,DEV=0",
  "output_device": "plughw:CARD=seeed2micvoicec,DEV=0"
}
```

Then `sudo systemctl restart little-voicemail`.

Set the mic gain and speaker volume with `alsamixer` (F6 to pick the card).
Save them so they survive a reboot: `sudo alsactl store`.

## 7. Add contacts

**Contacts** in the web UI. For each button, enter a nickname and the person's
number in full international form — `+447700900123`, not `07700900123`. The
person must already use Signal on that number.

"Get nicknames from Signal" fills in names from the linked account's contact
list, so you do not have to type them twice.

## 8. Quiet times

**Quiet times** gives you three independent windows:

- **School** — weekdays 09:00–15:15
- **Nap** — a short afternoon window
- **Bedtime** — 19:00–07:00, wrapping past midnight

Each has its own on/off switch and its own days. While one is running the box
is asleep: no ringtone, no lights, and any button press just flashes all nine
lamps three times. Messages still arrive and queue up quietly, and appear on
the buttons the moment the window ends.

## 9. Try it

1. Press button 1. It lights steady.
2. Hold push-to-talk. The big lamp lights. Say something.
3. Let go. Both lamps go out; the voice note arrives on the other phone.
4. Reply from that phone. The ringtone plays and button 1 starts flashing.
5. Press button 1. It plays the reply, then stays lit for 30 seconds so the
   child can answer straight back.

---

## Updating

The web UI checks GitHub whenever it loads. If there is a newer version,
**System** shows the running and available versions and an **Update and
restart** button. It pulls, reinstalls dependencies and reboots. A failed
update rolls the checkout back to where it started.

## Troubleshooting

**Buttons and lights show "simulated"**
I²C is not working. `i2cdetect -y 1` should show `20`. If it shows nothing,
check `dtparam=i2c_arm=on` in `/boot/firmware/config.txt` and reboot. If it
shows some other address, check A0/A1/A2 (pins 15-17) are all tied to GND.

**Phantom presses, or lamps flickering on their own**
The `RESET` pin (18) on the MCP23017 is floating. Tie it to 3V3.

**Lamps dim when several are lit**
Power. Use the official 3 A supply. The lamps run from the header's +5 V, so
check that rail rather than 3V3.

**Lamps are all on when they should be off, or vice versa**
The lamps are active low — anode to +5 V through the resistor, cathode to the
expander pin. Wiring them the other way round inverts the whole panel.

**"signal-cli is not installed" or voice notes arrive as file attachments**
You are on a signal-cli older than 0.14.2, before `--voice-note` existed.
`signal-cli --version` to check; re-run the installer to get 0.14.6.

**Recording is very quiet**
`alsamixer`, F6, pick the ReSpeaker card, raise the capture gain, then
`sudo alsactl store`.

**Nothing happens when a message arrives**
Check a quiet time is not running — the Status page says so at the top. Then
`journalctl -u little-voicemail -f` and watch while you send one.

**The lights stay on after reading on a phone**
Read receipts have to be enabled on the account: Signal → Settings → Privacy →
**Read receipts**. Without them, Signal never tells the Pi the message was
read. You can always clear a queue from the web UI instead.

**Locked out of the web UI**
```bash
sudo -u voicemail python3 -c "
from src.config import Config
Config('/etc/little-voicemail/config.json').set('', 'web', 'password_hash')"
```
Then reload the page to set a new one.
