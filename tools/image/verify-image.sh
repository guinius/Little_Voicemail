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
pass() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m  !!\033[0m %s\n' "$*" >&2; BAD=1; }

check() {
    local what="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi
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
else
    echo "  .. skipping the java and signal-cli run checks under emulation"
fi
check "the checkout is a git repo, so updates work" \
    git -C /opt/little-voicemail rev-parse HEAD

# -- boot configuration -----------------------------------------------------
check "the ReSpeaker overlay ships with the base image" \
    test -f /boot/firmware/overlays/seeed-2mic-voicecard.dtbo
check "the ReSpeaker overlay is enabled" \
    grep -q '^dtoverlay=seeed-2mic-voicecard' /boot/firmware/config.txt
check "I2C is enabled" grep -q '^dtparam=i2c_arm=on' /boot/firmware/config.txt

# Raspberry Pi's own first-boot hook grows the root filesystem to fill the
# card and applies Imager's customisation. Losing it would ship an image that
# never expands and cannot be given WiFi from Imager.
check "Raspberry Pi's firstboot hook is intact" \
    grep -q 'firstboot' /boot/firmware/cmdline.txt

# -- services ---------------------------------------------------------------
for unit in little-voicemail-web little-voicemail-portal little-voicemail \
            signal-cli little-voicemail-firstboot; do
    check "$unit.service is installed" test -f "/etc/systemd/system/$unit.service"
done

for unit in little-voicemail-web little-voicemail-portal \
            little-voicemail-firstboot; do
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
        /etc/systemd/system/signal-cli.service
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
