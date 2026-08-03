#!/usr/bin/env bash
#
# Little Voicemail installer for Raspberry Pi OS (Bookworm or newer).
#
#   curl -fsSL https://raw.githubusercontent.com/guinius/Little_Voicemail/master/install.sh | sudo bash
#
# or, from a checkout:
#
#   sudo ./install.sh
#
# Most people should flash the prebuilt image instead - it is this script
# already run. See SETUP.md. This is the path for an existing Pi.
#
# LV_IMAGE_BUILD=1 puts the script in image-build mode: it is running inside
# a chroot on a build machine, where there is no hardware to probe and no
# running systemd to talk to.
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/guinius/Little_Voicemail.git}"
BRANCH="${BRANCH:-master}"
INSTALL_DIR="/opt/little-voicemail"
CONFIG_DIR="/etc/little-voicemail"
DATA_DIR="/var/lib/little-voicemail"
HELPER_DIR="/usr/local/lib/little-voicemail"
BOOT_CONFIG="${LV_BOOT_CONFIG:-/boot/firmware/config.txt}"
SERVICE_USER="voicemail"
SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.6}"
SYSTEMCTL="/usr/bin/systemctl"

log()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

image_build() { [[ "${LV_IMAGE_BUILD:-0}" == "1" ]]; }

[[ $EUID -eq 0 ]] || die "run this with sudo"
image_build && log "image-build mode: no hardware probing, no service starts"

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------- packages
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev \
    git curl ffmpeg alsa-utils \
    i2c-tools libasound2-dev \
    avahi-daemon \
    network-manager \
    openjdk-21-jre-headless
ok "packages installed"

# --------------------------------------------------------- boot config.txt
# Both of these are device-tree settings that only take effect after a
# reboot, and both are needed before any of the hardware works: the codec
# for audio, I2C for the buttons and lights.
log "Configuring the boot overlay"
if [[ -f "$BOOT_CONFIG" ]]; then
    grep -q '^dtparam=i2c_arm=on' "$BOOT_CONFIG" \
        || echo 'dtparam=i2c_arm=on' >> "$BOOT_CONFIG"
    # On Bookworm and later the ReSpeaker driver is just this overlay, which
    # ships with the OS. Writing it here rather than asking for it by hand
    # removes the one genuinely manual step in the whole install.
    grep -q '^dtoverlay=seeed-2mic-voicecard' "$BOOT_CONFIG" \
        || echo 'dtoverlay=seeed-2mic-voicecard' >> "$BOOT_CONFIG"
    ok "i2c and the ReSpeaker overlay are enabled in $BOOT_CONFIG"
else
    log "  no $BOOT_CONFIG (not a Raspberry Pi?); skipping"
fi
grep -q '^i2c-dev' /etc/modules 2>/dev/null || echo 'i2c-dev' >> /etc/modules

if ! image_build && command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0 || true
fi

# ------------------------------------------------------- respeaker driver
if image_build; then
    # A chroot has no sound cards to look at, so check the overlay the image
    # will need is actually present. Raspberry Pi OS ships it; if a future
    # release drops it, the image would boot with no sound card and nobody
    # would find out until a Pi was in hand.
    [[ -f /boot/firmware/overlays/seeed-2mic-voicecard.dtbo ]] \
        || die "seeed-2mic-voicecard.dtbo is missing from this base image"
    ok "ReSpeaker overlay present in the base image"
else
    log "Checking the ReSpeaker 2-Mic HAT"
    if aplay -l 2>/dev/null | grep -qi 'seeed\|wm8960\|tlv320'; then
        ok "ReSpeaker detected"
    else
        echo "  Not detected yet. The overlay has just been enabled, so reboot"
        echo "  and check again with: aplay -l"
    fi
fi

# ------------------------------------------------------------------- user
log "Creating the service user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$DATA_DIR" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
fi
for group in audio i2c gpio netdev; do
    getent group "$group" >/dev/null && usermod -aG "$group" "$SERVICE_USER"
done
ok "user '$SERVICE_USER' ready"

# -------------------------------------------------------------- signal-cli
log "Installing signal-cli $SIGNAL_CLI_VERSION"
# Deliberately a directory test rather than running `signal-cli --version`:
# under image build that would start a JVM inside an emulated chroot, which
# is slow at best and a segfault at worst.
if [[ ! -d "/opt/signal-cli-${SIGNAL_CLI_VERSION}" ]]; then
    tmp="$(mktemp -d)"
    tarball="${LV_SIGNAL_CLI_TARBALL:-}"
    if [[ -z "$tarball" ]]; then
        url="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
        curl -fsSL --retry 3 "$url" -o "$tmp/signal-cli.tar.gz" \
            || die "could not download signal-cli $SIGNAL_CLI_VERSION"
        tarball="$tmp/signal-cli.tar.gz"
    fi
    tar -xzf "$tarball" -C /opt
    rm -rf "$tmp"
fi
ln -sf "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli
# --voice-note landed in 0.14.2; without it we cannot send real voice notes.
[[ -x /usr/local/bin/signal-cli ]] || die "signal-cli did not install"
ok "signal-cli $SIGNAL_CLI_VERSION installed"

# ------------------------------------------------------------------- code
log "Installing Little Voicemail"
# A local checkout wins over whatever is already installed: that is what the
# image builder hands us, and it is also what someone running ./install.sh
# from a working copy means.
if [[ -f "$SOURCE_DIR/src/main.py" ]]; then
    mkdir -p "$INSTALL_DIR"
    # The trailing /. copies dotfiles, .git among them - the self-updater
    # needs a real checkout to fast-forward.
    cp -r "$SOURCE_DIR/." "$INSTALL_DIR/"
elif [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --all --prune
    git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
    # CI checkouts carry a credential header and a narrowed refspec. The
    # header would ship a token inside every copy of the image; the refspec
    # would make `git reset --hard origin/master` fail on the device.
    git -C "$INSTALL_DIR" config --unset-all "http.https://github.com/.extraheader" \
        2>/dev/null || true
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL" 2>/dev/null || true
    git -C "$INSTALL_DIR" config --replace-all remote.origin.fetch \
        '+refs/heads/*:refs/remotes/origin/*' 2>/dev/null || true
    git config --system --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    image_build && git -C "$INSTALL_DIR" gc --quiet --prune=now 2>/dev/null || true
fi

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q --retries 5 --timeout 60 \
    -r "$INSTALL_DIR/requirements.txt"
ok "code installed at $INSTALL_DIR"

# ------------------------------------------------------------ directories
log "Creating directories"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"/{recordings,certs,signal-cli}
[[ -f "$CONFIG_DIR/config.json" ]] || \
    cp "$INSTALL_DIR/config/config.example.json" "$CONFIG_DIR/config.json"
[[ -f "$CONFIG_DIR/signal.env" ]] || \
    echo 'SIGNAL_ACCOUNT=' > "$CONFIG_DIR/signal.env"

chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR" "$INSTALL_DIR"
chmod 750 "$CONFIG_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR/signal-cli"
chmod 640 "$CONFIG_DIR/signal.env"
ok "directories ready"

# ---------------------------------------------------------------- helpers
log "Installing the network helper"
install -d -m 0755 "$HELPER_DIR"
install -m 0755 -o root -g root "$INSTALL_DIR/tools/image/lv-netctl" \
    "$HELPER_DIR/lv-netctl"
# NetworkManager's shared mode runs a dnsmasq for the setup access point and
# reads extra settings from here. Pointing every name at the Pi is what makes
# a phone show the "Sign in to network" prompt by itself.
if [[ -d /etc/NetworkManager ]]; then
    install -d -m 0755 /etc/NetworkManager/dnsmasq-shared.d
    install -m 0644 "$INSTALL_DIR/tools/image/lv-portal-dnsmasq.conf" \
        /etc/NetworkManager/dnsmasq-shared.d/lv-portal.conf
fi
ok "lv-netctl installed"

# ---------------------------------------------------------------- sudoers
log "Granting the web UI the few root actions it needs"
# sudo matches the whole argument vector, so these have to be written exactly
# as src/signal_link.py and src/web/portal.py assemble them.
sudoers="/etc/sudoers.d/little-voicemail"
tmp_sudoers="$(mktemp)"
cat > "$tmp_sudoers" <<EOF
# Installed by Little Voicemail. Each line is one exact command line.
$SERVICE_USER ALL=(root) NOPASSWD: /sbin/reboot
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL enable --now signal-cli.service little-voicemail.service
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL disable --now signal-cli.service little-voicemail.service
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart signal-cli.service little-voicemail.service
$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop signal-cli.service
$SERVICE_USER ALL=(root) NOPASSWD: $HELPER_DIR/lv-netctl
EOF
chmod 440 "$tmp_sudoers"
# A malformed drop-in breaks sudo for the whole machine, on a box with no
# screen and possibly no SSH. Never install one without checking it first.
if visudo -cf "$tmp_sudoers" >/dev/null; then
    install -m 0440 -o root -g root "$tmp_sudoers" "$sudoers"
    rm -f "$tmp_sudoers"
    ok "sudoers rule installed"
else
    rm -f "$tmp_sudoers"
    die "the generated sudoers file is invalid; refusing to install it"
fi

# --------------------------------------------------------------- services
log "Installing systemd services"
cp "$INSTALL_DIR"/services/*.service /etc/systemd/system/

# The phone and signal-cli services need a linked account before they can do
# anything, so only the web UI and the setup portal come up now. The Signal
# tab in the web UI enables the other two once linking succeeds.
enable_now=(little-voicemail-web.service little-voicemail-portal.service)

if image_build; then
    # No systemd is running in a chroot, so `enable` can only be asked to do
    # its filesystem half.
    mkdir -p /etc/systemd/system/multi-user.target.wants
    for unit in "${enable_now[@]}"; do
        SYSTEMD_OFFLINE=1 systemctl enable "$unit" >/dev/null 2>&1 \
            || ln -sf "/etc/systemd/system/$unit" \
                 "/etc/systemd/system/multi-user.target.wants/$unit"
    done
    ok "services installed and enabled offline"
else
    systemctl daemon-reload
    systemctl enable "${enable_now[@]}" >/dev/null
    systemctl restart "${enable_now[@]}"
    ok "services installed and started"
fi

if image_build; then
    cat <<EOF

  Little Voicemail is baked into the image.

  Nothing is running yet - that happens on the real device at first boot.

EOF
    exit 0
fi

hostname_short="$(hostname -s)"
address="$(hostname -I | awk '{print $1}')"
port="$(python3 - "$CONFIG_DIR/config.json" <<'PY' 2>/dev/null || echo 8443
import json, sys
print(json.load(open(sys.argv[1]))["web"]["port"])
PY
)"

cat <<EOF

  Little Voicemail is installed.

  1. Open the parent settings and choose a password:

         https://${hostname_short}.local:${port}     (or https://${address}:${port})

     The certificate is self-signed, so your browser will warn you once.

  2. Link a Signal account from the Signal tab. No SSH, no QR code needed
     on Android - it is one tap.

  A reboot is needed if the ReSpeaker overlay or I2C were just enabled.

  Full walkthrough: $INSTALL_DIR/SETUP.md

EOF
