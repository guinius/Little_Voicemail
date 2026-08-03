#!/usr/bin/env bash
#
# Build a Raspberry Pi OS Lite 64-bit image with Little Voicemail already
# installed, the ReSpeaker overlay already enabled, and the web UI already
# enabled - so flashing it and powering on is the whole of the setup.
#
#   sudo tools/build-image.sh --work-dir /var/tmp/lvbuild --output-dir dist
#
# It customises the official image rather than building an OS from scratch:
# pi-gen takes hours and drifts from upstream, and everything that makes this
# a Raspberry Pi OS image is something we want to leave exactly alone. In
# particular Raspberry Pi Imager's OS customisation - hostname, WiFi, SSH -
# keeps working on the result, because none of the machinery behind it is
# touched.
#
# Build on arm64 and no emulation is involved at all. On x86 it needs
# qemu-user-static with binfmt registered.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=tools/image/lib.sh
source "$HERE/image/lib.sh"

BASE_URL_DEFAULT="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2024-11-19/2024-11-19-raspios-bookworm-arm64-lite.img.xz"
SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.7}"

BASE_URL="${LV_BASE_URL:-$BASE_URL_DEFAULT}"
SRC_DIR="$REPO_ROOT"
WORK_DIR="/var/tmp/lv-image-build"
OUTPUT_DIR="$REPO_ROOT/dist"
GROW_BY="3G"
DO_SHRINK=1
DO_COMPRESS=1
KEEP_MOUNTS=0

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --base-url URL     Raspberry Pi OS Lite arm64 .img.xz to start from
  --src DIR          checkout to install (default: this repo)
  --work-dir DIR     scratch space; needs ~12 GB free
  --output-dir DIR   where the .img.xz and .sha256 land
  --grow SIZE        extra room for the root filesystem (default 3G)
  --no-shrink        skip shrinking the image back down
  --no-compress      leave a raw .img instead of .img.xz
  --keep-mounts      leave the image mounted for poking around
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url)   BASE_URL="$2"; shift 2 ;;
        --src)        SRC_DIR="$(cd "$2" && pwd)"; shift 2 ;;
        --work-dir)   WORK_DIR="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --grow)       GROW_BY="$2"; shift 2 ;;
        --no-shrink)  DO_SHRINK=0; shift ;;
        --no-compress) DO_COMPRESS=0; shift ;;
        --keep-mounts) KEEP_MOUNTS=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown option: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run this with sudo (it needs losetup, mount and chroot)"

need curl xz parted losetup mount chroot rsync sha256sum truncate
need e2fsck resize2fs dumpe2fs

# ------------------------------------------------------------------ qemu
NEED_QEMU=0
QEMU_BIN=""
if [[ "$(uname -m)" != "aarch64" ]]; then
    NEED_QEMU=1
    QEMU_BIN="$(command -v qemu-aarch64-static || true)"
    [[ -n "$QEMU_BIN" ]] \
        || die "install qemu-user-static, or build on an arm64 machine"
    [[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]] \
        || die "arm64 binfmt is not registered (try: apt install binfmt-support, \
or docker run --privileged --rm tonistiigi/binfmt --install arm64)"
    warn "building under qemu emulation; this is several times slower than arm64"
fi
# Native means the chroot can run arm64 binaries at full speed, so the verify
# step is free to actually start a JVM and signal-cli. Under emulation that is
# slow at best and a segfault at worst, so those checks are skipped.
LV_NATIVE=$(( NEED_QEMU == 1 ? 0 : 1 ))
export NEED_QEMU QEMU_BIN LV_NATIVE

trap cleanup EXIT INT TERM

VERSION="$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo 0.0.0)"
STAMP="$(date -u +%Y-%m-%d)"
NAME="little-voicemail-${VERSION}-${STAMP}-arm64"

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
ROOT="$WORK_DIR/mnt"
IMAGE="$WORK_DIR/$NAME.img"
BASE_XZ="$WORK_DIR/$(basename "$BASE_URL")"

# ------------------------------------------------------------------- base
log "Fetching the base image"
if [[ ! -f "$BASE_XZ" ]]; then
    curl -fL --retry 3 -o "$BASE_XZ.part" "$BASE_URL" || die "could not download the base image"
    mv "$BASE_XZ.part" "$BASE_XZ"
fi
if curl -fsSL --retry 2 "$BASE_URL.sha256" -o "$BASE_XZ.sha256" 2>/dev/null; then
    (cd "$WORK_DIR" && sha256sum -c "$(basename "$BASE_XZ").sha256") \
        || die "the base image failed its checksum"
    ok "base image checksum verified"
else
    warn "no published checksum for the base image; continuing unverified"
fi

log "Unpacking"
rm -f "$IMAGE"
xz -dcT0 "$BASE_XZ" > "$IMAGE"
ok "$(du -h "$IMAGE" | cut -f1) unpacked"

# ------------------------------------------------------------------- grow
log "Growing the root filesystem by $GROW_BY"
# The stock rootfs has no headroom for a JRE, ffmpeg and signal-cli.
DISKID_BEFORE="$(fdisk -l "$IMAGE" | awk '/Disk identifier/ {print $3}')"
truncate -s "+$GROW_BY" "$IMAGE"
parted -s "$IMAGE" resizepart 2 100%
DISKID_AFTER="$(fdisk -l "$IMAGE" | awk '/Disk identifier/ {print $3}')"
# cmdline.txt says root=PARTUUID=<diskid>-02. A parted that recreates the
# partition instead of resizing it changes that and produces an image that
# boots to a kernel panic - with no console to see it on.
[[ "$DISKID_BEFORE" == "$DISKID_AFTER" ]] \
    || die "the disk identifier changed ($DISKID_BEFORE -> $DISKID_AFTER); \
cmdline.txt's root=PARTUUID would no longer match"

attach_loop "$IMAGE" >/dev/null
e2fsck -pf "${LOOP}p2" || true
resize2fs "${LOOP}p2"
ok "root filesystem grown"

# ------------------------------------------------------------------ build
log "Mounting and entering the image"
mount_image "$ROOT"
enter_chroot "$ROOT"

log "Staging the checkout"
mkdir -p "$ROOT/usr/local/src/lv-build"
rsync -a --delete \
    --exclude '.venv' --exclude 'dist' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude 'var' \
    "$SRC_DIR/" "$ROOT/usr/local/src/lv-build/"

# Fetch signal-cli on the host: it is 60 MB, and pulling it inside an
# emulated chroot is needlessly slow.
TARBALL="$WORK_DIR/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
if [[ ! -f "$TARBALL" ]]; then
    log "Fetching signal-cli $SIGNAL_CLI_VERSION"
    curl -fsSL --retry 3 -o "$TARBALL.part" \
        "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
        || die "could not download signal-cli"
    mv "$TARBALL.part" "$TARBALL"
fi
cp "$TARBALL" "$ROOT/usr/local/src/signal-cli.tar.gz"

log "Installing Little Voicemail into the image"
in_chroot "$ROOT" \
    env LV_SIGNAL_CLI_TARBALL=/usr/local/src/signal-cli.tar.gz \
        SIGNAL_CLI_VERSION="$SIGNAL_CLI_VERSION" \
    bash /usr/local/src/lv-build/tools/image/chroot-setup.sh \
    || die "the in-image install failed"

log "Verifying"
in_chroot "$ROOT" bash /usr/local/src/lv-build/tools/image/verify-image.sh \
    || die "the image failed verification"

log "Removing everything device-specific"
in_chroot "$ROOT" bash /usr/local/src/lv-build/tools/image/cleanup.sh \
    || die "the image could not be de-personalised"
# From out here, so cleanup.sh is not deleting the file it is running from.
rm -rf "$ROOT/usr/local/src/lv-build" "$ROOT/usr/local/src/signal-cli.tar.gz"

if [[ "$KEEP_MOUNTS" == "1" ]]; then
    ok "left mounted at $ROOT (loop $LOOP); unmount it yourself"
    trap - EXIT INT TERM
    exit 0
fi

leave_chroot "$ROOT"
umount -R "$ROOT"
MOUNTED=()
ok "image built"

# ----------------------------------------------------------------- shrink
if [[ "$DO_SHRINK" == "1" ]]; then
    log "Shrinking"
    e2fsck -pf "${LOOP}p2" || true
    if command -v zerofree >/dev/null 2>&1; then
        zerofree "${LOOP}p2" || warn "zerofree failed; the image will compress worse"
    fi
    resize2fs -M "${LOOP}p2" >/dev/null
    e2fsck -pf "${LOOP}p2" || true

    block_size="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/Block size/ {print $2+0}')"
    block_count="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/Block count/ {print $2+0}')"
    # Leave a little slack rather than sitting at the absolute minimum; the
    # real expansion happens on the SD card at first boot anyway.
    slack_blocks=$(( 64 * 1024 * 1024 / block_size ))
    resize2fs "${LOOP}p2" $(( block_count + slack_blocks )) >/dev/null
    block_count=$(( block_count + slack_blocks ))

    part_start="$(parted -sm "$IMAGE" unit s print | awk -F: '/^2:/ {print $2+0}')"
    fs_sectors=$(( block_count * block_size / 512 ))
    part_end=$(( part_start + fs_sectors - 1 ))

    detach_loop
    parted -s "$IMAGE" unit s resizepart 2 "$part_end"
    truncate -s $(( (part_end + 1) * 512 )) "$IMAGE"

    attach_loop "$IMAGE" >/dev/null
    e2fsck -pf "${LOOP}p2" || die "the shrunk filesystem is not clean"
    detach_loop
    ok "shrunk to $(du -h "$IMAGE" | cut -f1)"
else
    detach_loop
fi

# --------------------------------------------------------------- compress
mkdir -p "$OUTPUT_DIR"
if [[ "$DO_COMPRESS" == "1" ]]; then
    log "Compressing (this is the slow part)"
    rm -f "$OUTPUT_DIR/$NAME.img.xz"
    xz -T0 -9 -c "$IMAGE" > "$OUTPUT_DIR/$NAME.img.xz"
    (cd "$OUTPUT_DIR" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256")
    ARTEFACT="$OUTPUT_DIR/$NAME.img.xz"
else
    mv "$IMAGE" "$OUTPUT_DIR/$NAME.img"
    (cd "$OUTPUT_DIR" && sha256sum "$NAME.img" > "$NAME.img.sha256")
    ARTEFACT="$OUTPUT_DIR/$NAME.img"
fi

cat <<EOF

  $(du -h "$ARTEFACT" | cut -f1)  $ARTEFACT

  Flash it with Raspberry Pi Imager ("Use custom"). Imager's OS customisation
  still works on this image, so set the WiFi there. If you do not, the box
  raises its own "Little Voicemail setup" network on first boot instead.

EOF
