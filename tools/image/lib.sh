# Shared helpers for the image build. Sourced, not executed.
#
# The important thing here is the cleanup trap: a build that dies with a loop
# device still attached wedges a CI runner, and locally it holds a multi-gigabyte
# file open until reboot.

log()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;35m  ..\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

need() {
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || die "missing tool: $tool"
    done
}

LOOP=""
MOUNTED=()

track_mount() { MOUNTED=("$1" "${MOUNTED[@]+"${MOUNTED[@]}"}"); }

cleanup() {
    local status=$?
    set +e
    if [[ ${#MOUNTED[@]} -gt 0 ]]; then
        for point in "${MOUNTED[@]}"; do
            mountpoint -q "$point" && umount -R "$point" 2>/dev/null
            mountpoint -q "$point" && umount -lR "$point" 2>/dev/null
        done
    fi
    if [[ -n "$LOOP" ]] && losetup "$LOOP" >/dev/null 2>&1; then
        losetup -d "$LOOP" 2>/dev/null
    fi
    MOUNTED=()
    LOOP=""
    return $status
}

attach_loop() {
    local image="$1"
    LOOP="$(losetup --show -fP "$image")" || die "could not attach $image"
    # Partition nodes can lag the ioctl on a busy machine.
    for _ in $(seq 1 25); do
        [[ -b "${LOOP}p2" ]] && break
        sleep 0.2
    done
    [[ -b "${LOOP}p2" ]] || die "no partitions appeared for $LOOP"
    echo "$LOOP"
}

detach_loop() {
    [[ -n "$LOOP" ]] || return 0
    losetup -d "$LOOP" 2>/dev/null
    LOOP=""
}

mount_image() {
    local root="$1"
    mkdir -p "$root"
    mount "${LOOP}p2" "$root" || die "could not mount the root partition"
    track_mount "$root"
    mkdir -p "$root/boot/firmware"
    mount "${LOOP}p1" "$root/boot/firmware" || die "could not mount the boot partition"
}

enter_chroot() {
    local root="$1"
    for dir in /dev /dev/pts /proc /sys; do
        mkdir -p "$root$dir"
        mount --bind "$dir" "$root$dir" || die "could not bind $dir"
    done
    # Deliberately NOT a bind of the host's /run: that hands the chroot a
    # socket to the build machine's PID 1, and `systemctl enable` inside would
    # then quietly enable units on the build machine instead of in the image.
    mkdir -p "$root/run"
    mount -t tmpfs tmpfs "$root/run"

    # Raspberry Pi OS ships /etc/resolv.conf as a symlink into /run, which the
    # tmpfs above just emptied. Put a real one in for the duration.
    if [[ -e "$root/etc/resolv.conf" || -L "$root/etc/resolv.conf" ]]; then
        mv "$root/etc/resolv.conf" "$root/etc/resolv.conf.lvbak"
    fi
    printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$root/etc/resolv.conf"

    # Stop apt postinsts trying to start daemons that have no systemd to talk to.
    printf '#!/bin/sh\nexit 101\n' > "$root/usr/sbin/policy-rc.d"
    chmod 0755 "$root/usr/sbin/policy-rc.d"

    # Some postinsts want a machine-id to exist. It is zeroed again at cleanup.
    [[ -s "$root/etc/machine-id" ]] || \
        printf '0123456789abcdef0123456789abcdef\n' > "$root/etc/machine-id"

    if [[ "${NEED_QEMU:-0}" == "1" ]]; then
        cp "$QEMU_BIN" "$root/usr/bin/$(basename "$QEMU_BIN")"
    fi
}

leave_chroot() {
    local root="$1"
    # Written with explicit ifs: under `set -e` a bare `test && action` aborts
    # the whole build whenever the test is simply false.
    rm -f "$root/usr/sbin/policy-rc.d"
    if [[ "${NEED_QEMU:-0}" == "1" ]]; then
        rm -f "$root/usr/bin/$(basename "$QEMU_BIN")"
    fi
    rm -f "$root/etc/resolv.conf"
    if [[ -e "$root/etc/resolv.conf.lvbak" || -L "$root/etc/resolv.conf.lvbak" ]]; then
        mv "$root/etc/resolv.conf.lvbak" "$root/etc/resolv.conf"
    fi
    for dir in /run /sys /proc /dev/pts /dev; do
        if mountpoint -q "$root$dir"; then
            umount -l "$root$dir" || true
        fi
    done
    return 0
}

in_chroot() {
    local root="$1"; shift
    chroot "$root" env \
        DEBIAN_FRONTEND=noninteractive LC_ALL=C LANG=C \
        LV_NATIVE="${LV_NATIVE:-0}" \
        LV_AUDIO_OVERLAY="${LV_AUDIO_OVERLAY:-respeaker-2mic-v2_0}" \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        "$@"
}
