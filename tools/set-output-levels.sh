#!/usr/bin/env bash
# Push every playback volume control on the ReSpeaker HAT's codec to
# maximum, and unmute every playback switch.
#
# Nothing else in this project ever touches these - the kernel driver's own
# defaults leave real headroom unused on every boot (on the TLV320AIC3104,
# for instance, PCM/HP DAC/Line DAC all come up around -23.5 dB below their
# own maximum), which is quiet enough to be mistaken for a hardware fault.
# See HARDWARE.md.
#
# Deliberately generic rather than hardcoding TLV320AIC3104 control names:
# it discovers every "* Playback Volume" / "* Playback Switch" control the
# card actually exposes and maxes/unmutes each one, so the same script
# works whether install.sh chose the v2.0 (TLV320AIC3104) or v1.0 (WM8960)
# overlay - see LV_AUDIO_OVERLAY. Capture (microphone) controls are
# untouched on purpose.
set -euo pipefail

# The card can take a moment to enumerate right after boot even though this
# unit orders itself after sound.target - retry rather than run once and
# silently do nothing on a slow boot.
card=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    card="$(aplay -l 2>/dev/null \
        | awk -F'[ :]+' '/seeed|wm8960|tlv320/{print $2; exit}')"
    [[ -n "$card" ]] && break
    sleep 1
done

if [[ -z "$card" ]]; then
    echo "no ReSpeaker sound card found after 10s; nothing to do" >&2
    exit 0
fi

set_all() {
    local suffix="$1" value="$2"
    amixer -c "$card" controls 2>/dev/null \
        | sed -n "s/.*name='\(.*${suffix}\)'.*/\1/p" \
        | while IFS= read -r control; do
            amixer -c "$card" sset "$control" "$value" >/dev/null 2>&1 || true
        done
}

set_all "Playback Volume" 100%
set_all "Playback Switch" on

echo "levels set to maximum on card $card"
