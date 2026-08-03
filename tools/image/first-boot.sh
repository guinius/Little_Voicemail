#!/usr/bin/env bash
#
# Installed as /usr/local/sbin/lv-first-boot and run once, by
# little-voicemail-firstboot.service, the first time the real device boots.
#
# Only genuinely device-specific things belong here. Anything that can be
# decided at build time was decided at build time, and anything the code
# already creates lazily - the TLS certificate, the session key, the machine
# id, the SSH host keys - is left alone deliberately.
#
set -uo pipefail

CONFIG="/etc/little-voicemail/config.json"
DATA_DIR="/var/lib/little-voicemail"
STAMP="$DATA_DIR/.first-boot-done"
VENV_PYTHON="/opt/little-voicemail/.venv/bin/python"

log() { printf 'lv-first-boot: %s\n' "$*"; }

[[ -e "$STAMP" ]] && { log "already done"; exit 0; }

# -- audio ------------------------------------------------------------------
# Recordings that are inaudibly quiet are the most likely first complaint, and
# the ReSpeaker comes up with its capture gain near zero.
card="$(aplay -l 2>/dev/null \
        | sed -n 's/^card \([0-9]\+\): \([^ ]*\) .*/\1 \2/p' \
        | grep -iv 'bcm2835\|vc4\|hdmi' | head -n1)"

if [[ -n "$card" ]]; then
    index="${card%% *}"
    name="${card##* }"
    log "sound card $index ($name)"

    for control in Capture ADC "ADC PCM" "Left Input Boost Mixer LINPUT1" \
                   "Right Input Boost Mixer RINPUT1"; do
        amixer -c "$index" sset "$control" 80% unmute >/dev/null 2>&1
    done
    for control in Speaker Headphone PCM Playback; do
        amixer -c "$index" sset "$control" 85% unmute >/dev/null 2>&1
    done
    alsactl store >/dev/null 2>&1 && log "mixer settings saved"

    # ReSpeaker v1 and v2 boards do not use the same ALSA card name, so take
    # whatever this board actually calls itself rather than trusting the default.
    if ! aplay -L 2>/dev/null | grep -q 'CARD=seeed2micvoicec'; then
        log "card is not named seeed2micvoicec; writing '$name' into the config"
        "$VENV_PYTHON" - "$CONFIG" "$name" <<'PY' || log "could not update the config"
import sys
sys.path.insert(0, "/opt/little-voicemail")
from src.config import Config

config_path, card = sys.argv[1], sys.argv[2]
config = Config(config_path)
device = f"plughw:CARD={card},DEV=0"
config.set(device, "audio", "input_device")
config.set(device, "audio", "output_device")
PY
    fi
else
    log "no sound card found; leaving the audio config alone"
fi

# -- hostname ---------------------------------------------------------------
# Raspberry Pi Imager can set a hostname the config file has never heard of,
# and the TLS certificate is built from the config's idea of it.
"$VENV_PYTHON" - "$CONFIG" <<'PY' || log "could not sync the hostname"
import socket
import sys
sys.path.insert(0, "/opt/little-voicemail")
from src.config import Config

config = Config(sys.argv[1])
name = socket.gethostname()
if name and name != config.get("web", "hostname", default=""):
    config.set(name, "web", "hostname")
    print(f"lv-first-boot: hostname is {name}")
PY

chown -R voicemail:voicemail "$CONFIG" "$DATA_DIR" 2>/dev/null || true
mkdir -p "$DATA_DIR"
touch "$STAMP"
log "done"
exit 0
