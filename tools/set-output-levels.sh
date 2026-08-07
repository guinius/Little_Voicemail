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
#
# This uses the *raw* control interface throughout (amixer controls/cget/
# cset, addressed by numid) rather than the "simple" mixer interface
# (scontrols/sset). They're two different ALSA APIs: sset only recognises
# controls that map onto its simplified per-device names, and on this
# codec it doesn't recognise perfectly real controls that `amixer controls`
# lists just fine ("Unable to find simple control 'PCM Playback Volume',0")
# - it just doesn't know that name. cget/cset by numid always works, at
# the cost of doing the value read/max-substitution by hand instead of
# getting sset's "100%"/"on" convenience for free.
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

# Replace every value on a control's "values=..." line with $2, preserving
# however many comma-separated channels it has (1, 2, whatever) - reading
# the current line and substituting into it sidesteps having to separately
# work out the channel count and rebuild a matching value list by hand.
set_control() {
    local numid="$1" replacement="$2" pattern="$3"
    local detail current new
    detail="$(amixer -c "$card" cget numid="$numid" 2>/dev/null)"
    current="$(sed -n 's/^  : values=//p' <<<"$detail")"
    [[ -n "$current" ]] || return 0
    new="$(sed -E "s/${pattern}/${replacement}/g" <<<"$current")"
    amixer -c "$card" cset "numid=$numid" "$new" >/dev/null 2>&1 || true
}

amixer -c "$card" controls 2>/dev/null | while IFS= read -r line; do
    numid="${line#numid=}"
    numid="${numid%%,*}"
    name="$(sed -n "s/.*name='\(.*\)'.*/\1/p" <<<"$line")"

    case "$name" in
        *"Playback Volume")
            max="$(amixer -c "$card" cget numid="$numid" 2>/dev/null \
                | sed -n 's/.*max=\([0-9]*\).*/\1/p')"
            [[ -n "$max" ]] && set_control "$numid" "$max" '[0-9]+'
            ;;
        *"Playback Switch")
            set_control "$numid" "on" '[a-z]+'
            ;;
    esac
done

echo "levels set to maximum on card $card"
