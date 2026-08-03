#!/usr/bin/env bash
#
# Runs *inside* the image chroot, invoked by tools/build-image.sh.
# The repo checkout is at /usr/local/src/lv-build.
#
set -euo pipefail

SRC="/usr/local/src/lv-build"
log() { printf '\033[1;33m  chroot>\033[0m %s\n' "$*"; }

log "running the installer in image-build mode"
LV_IMAGE_BUILD=1 \
LV_SIGNAL_CLI_TARBALL="${LV_SIGNAL_CLI_TARBALL:-}" \
    bash "$SRC/install.sh"

log "installing the first-boot unit"
install -m 0755 -o root -g root "$SRC/tools/image/first-boot.sh" \
    /usr/local/sbin/lv-first-boot
install -m 0644 -o root -g root \
    "$SRC/tools/image/little-voicemail-firstboot.service" \
    /etc/systemd/system/little-voicemail-firstboot.service

mkdir -p /etc/systemd/system/multi-user.target.wants

# Ours lives in /etc/systemd/system, so the hand-made symlink is a valid
# fallback if systemctl cannot be persuaded to work offline.
SYSTEMD_OFFLINE=1 systemctl enable little-voicemail-firstboot.service >/dev/null 2>&1 \
    || ln -sf /etc/systemd/system/little-voicemail-firstboot.service \
         /etc/systemd/system/multi-user.target.wants/little-voicemail-firstboot.service

# These two ship with the OS and live in /lib/systemd/system, so there is no
# fallback to hand-roll - pointing at /etc/systemd/system would just make a
# broken link. mDNS is the whole access story, and NetworkManager is what the
# setup portal drives.
for unit in avahi-daemon.service NetworkManager.service; do
    SYSTEMD_OFFLINE=1 systemctl enable "$unit" >/dev/null 2>&1 \
        || log "could not enable $unit offline (it is usually enabled already)"
done

# mDNS is the whole access story for a box with no screen, so give it a name
# even when the flasher skips Raspberry Pi Imager's customisation. Imager
# overrides both of these when a hostname is set, which is what we want.
log "setting the default hostname"
echo "littlevoicemail" > /etc/hostname
if grep -q '^127.0.1.1' /etc/hosts; then
    sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tlittlevoicemail/' /etc/hosts
else
    printf '127.0.1.1\tlittlevoicemail\n' >> /etc/hosts
fi

log "chroot setup complete"
