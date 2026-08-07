#!/usr/bin/env bash
#
# Runs inside the chroot. Asserts the image is actually the thing we set out
# to build. These are the failures that really happen: a pip install that
# quietly did nothing under emulation, an overlay the base image no longer
# ships, a unit that was copied but never enabled.
#
# What this cannot check is whether the overlay loads, whether I2C enumerates,
# or whether audio works. Only a real Pi can tell you that.
#
set -uo pipefail

BAD=0
CHECK_EXIT=0
pass() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m  !!\033[0m %s\n' "$*" >&2; BAD=1; }

# A process bash reports as killed by signal N exits with 128+N. Turning that
# back into a name matters here specifically: a failing check with genuinely
# no output (see `check` below) is exactly what a crash - SIGSEGV, SIGILL,
# SIGABRT - looks like, and "exit 139" says a lot more than a blank line.
signal_name() {
    case "$1" in
        130) echo "SIGINT" ;;   131) echo "SIGQUIT" ;;
        132) echo "SIGILL" ;;   133) echo "SIGTRAP" ;;
        134) echo "SIGABRT" ;;  135) echo "SIGBUS" ;;
        136) echo "SIGFPE" ;;   137) echo "SIGKILL" ;;
        139) echo "SIGSEGV" ;;  141) echo "SIGPIPE" ;;
        *) echo "" ;;
    esac
}

check() {
    local what="$1" out trimmed; shift
    # Capture rather than discard: a failing check with no output tells you
    # only that something is wrong, which costs a whole build to diagnose.
    out="$("$@" 2>&1)"
    CHECK_EXIT=$?
    if [[ "$CHECK_EXIT" == "0" ]]; then
        pass "$what"
        return
    fi
    fail "$what"

    if [[ "$CHECK_EXIT" -ge 128 ]]; then
        # Bash swallows the shell's own "Segmentation fault" notice inside a
        # command substitution - it never reaches $out - so this exit range
        # is worth calling out even when real output follows below.
        local name
        name="$(signal_name "$CHECK_EXIT")"
        printf '     exit %s (%s)\n' "$CHECK_EXIT" \
            "${name:-signal $((CHECK_EXIT - 128))}" >&2
    fi

    # A crash banner or a stack trace commonly opens with a blank line for
    # visual separation. An earlier version of this script printed only
    # "the first line" of $out, which for exactly that shape of output prints
    # nothing at all and reads as "no output" - hiding the one thing this
    # check exists to surface. Strip only genuinely leading/trailing blank
    # lines, then print everything else, capped generously rather than to one
    # line.
    trimmed="$(printf '%s' "$out" | sed -e '/./,$!d')"
    if [[ -n "$trimmed" ]]; then
        printf '%s\n' "$trimmed" | tail -n 40 | sed 's/^/     /' >&2
    elif [[ "$CHECK_EXIT" -lt 128 ]]; then
        printf '     (no output; exit %s)\n' "$CHECK_EXIT" >&2
    fi
}

echo "-- verifying the image"

# -- the code actually installed -------------------------------------------
check "the venv exists" test -x /opt/little-voicemail/.venv/bin/python
check "every dependency imports" \
    /opt/little-voicemail/.venv/bin/python -c \
    "import flask, cheroot, cryptography, smbus2, segno"
check "the app imports" env \
    LV_CONFIG=/etc/little-voicemail/config.json \
    PYTHONPATH=/opt/little-voicemail \
    /opt/little-voicemail/.venv/bin/python -c \
    "import src.web.app, src.web.portal, src.signal_link"
check "signal-cli is on the path" test -x /usr/local/bin/signal-cli
check "the service user exists" id voicemail

# The one check that matters most, and the one that was missing when an image
# shipped with a JRE too old to run signal-cli at all: start them both. A
# wrong Java version installs perfectly happily and only fails later, on a
# device with no screen, the first time a parent tries to link an account.
#
# Only when native - under qemu a JVM is slow at best and a segfault at worst.
if [[ "${LV_NATIVE:-0}" == "1" ]]; then
    # grep the version line, not head -n1: JAVA_TOOL_OPTIONS makes the JVM
    # print "Picked up ..." first, which would misparse as a bad version.
    if java_line="$(java -version 2>&1 | grep -m1 'version "')"; then
        java_major="${java_line#*\"}"; java_major="${java_major%%\"*}"
        java_major="${java_major%%.*}"
        if [[ "$java_major" =~ ^[0-9]+$ ]] && [[ "$java_major" -ge 25 ]]; then
            pass "java $java_major is new enough for signal-cli"
        else
            fail "java reports '${java_line}'; signal-cli needs 25 or newer"
        fi
    else
        fail "java does not run"
    fi
    check "signal-cli actually starts" /usr/local/bin/signal-cli --version
    if [[ "$CHECK_EXIT" -ge 128 ]]; then
        # A crash this early is almost always the native libsignal_jni.so
        # we just spliced in, and the kernel's own report - exact fault
        # address, offending library - beats anything userspace can say
        # about it. Best-effort: some CI sandboxes restrict dmesg, so this
        # must never be what fails the build.
        echo "     recent kernel log (looking for the crash):" >&2
        dmesg 2>/dev/null | tail -30 | sed 's/^/     /' >&2 \
            || echo "     (dmesg unavailable in this environment)" >&2
    fi
else
    echo "  .. skipping the java and signal-cli run checks under emulation"
fi
check "the checkout is a git repo, so updates work" \
    git -C /opt/little-voicemail rev-parse HEAD

# -- boot configuration -----------------------------------------------------
# Which overlay the image was built for; install.sh writes it, and the two
# have to agree or the Pi boots with no sound card.
AUDIO_OVERLAY="${LV_AUDIO_OVERLAY:-respeaker-2mic-v2_0}"
check "the $AUDIO_OVERLAY overlay is installed" \
    test -f "/boot/firmware/overlays/$AUDIO_OVERLAY.dtbo"
check "the $AUDIO_OVERLAY overlay is enabled" \
    grep -q "^dtoverlay=$AUDIO_OVERLAY" /boot/firmware/config.txt
# Nothing should still be asking for the overlay that does not exist.
if grep -q '^dtoverlay=seeed-2mic-voicecard' /boot/firmware/config.txt; then
    fail "config.txt still names seeed-2mic-voicecard, which no image ships"
else
    pass "no stale seeed-2mic-voicecard line"
fi
check "I2C is enabled" grep -q '^dtparam=i2c_arm=on' /boot/firmware/config.txt

# Raspberry Pi's own first-boot hook grows the root filesystem to fill the
# card and applies Imager's customisation. Losing it would ship an image that
# never expands and cannot be given WiFi from Imager.
check "Raspberry Pi's firstboot hook is intact" \
    grep -q 'firstboot' /boot/firmware/cmdline.txt

# -- services ---------------------------------------------------------------
for unit in little-voicemail-web little-voicemail-portal little-voicemail \
            signal-cli little-voicemail-firstboot little-voicemail-audio-levels; do
    check "$unit.service is installed" test -f "/etc/systemd/system/$unit.service"
done

for unit in little-voicemail-web little-voicemail-portal \
            little-voicemail-firstboot little-voicemail-audio-levels; do
    check "$unit.service is enabled" \
        test -L "/etc/systemd/system/multi-user.target.wants/$unit.service"
done

# These two must NOT be enabled: there is no account in a fresh image, and
# starting them would be a crash loop on a box nobody can log in to.
for unit in little-voicemail signal-cli; do
    if [[ -L "/etc/systemd/system/multi-user.target.wants/$unit.service" ]]; then
        fail "$unit.service is enabled but there is no account yet"
    else
        pass "$unit.service is correctly left disabled"
    fi
done

if command -v systemd-analyze >/dev/null 2>&1; then
    check "the unit files parse" systemd-analyze verify \
        /etc/systemd/system/little-voicemail-web.service \
        /etc/systemd/system/little-voicemail-portal.service \
        /etc/systemd/system/signal-cli.service \
        /etc/systemd/system/little-voicemail-audio-levels.service
fi

# -- privileges -------------------------------------------------------------
# A malformed sudoers drop-in breaks sudo for the whole machine, on a device
# with no screen. This is the last chance to catch it.
check "the sudoers drop-in is valid" visudo -cf /etc/sudoers.d/little-voicemail
check "the network helper is installed" \
    test -x /usr/local/lib/little-voicemail/lv-netctl
check "the first-boot script is installed" test -x /usr/local/sbin/lv-first-boot

if [[ "$BAD" != "0" ]]; then
    printf '\033[1;31m!! the image did not verify\033[0m\n' >&2
    exit 1
fi
echo "-- image verified"
