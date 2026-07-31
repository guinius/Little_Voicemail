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
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/guinius/Little_Voicemail.git}"
BRANCH="${BRANCH:-master}"
INSTALL_DIR="/opt/little-voicemail"
CONFIG_DIR="/etc/little-voicemail"
DATA_DIR="/var/lib/little-voicemail"
SERVICE_USER="voicemail"
SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.6}"

log()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

# ---------------------------------------------------------------- packages
log "Installing system packages"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev \
    git curl ffmpeg alsa-utils \
    i2c-tools libasound2-dev \
    avahi-daemon \
    openjdk-21-jre-headless
ok "packages installed"

# ------------------------------------------------------------------- i2c
log "Enabling I2C"
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0 || true
fi
grep -q '^dtparam=i2c_arm=on' /boot/firmware/config.txt 2>/dev/null \
    || echo 'dtparam=i2c_arm=on' >> /boot/firmware/config.txt
grep -q '^i2c-dev' /etc/modules || echo 'i2c-dev' >> /etc/modules
ok "I2C enabled (a reboot applies it if it was off)"

# ------------------------------------------------------- respeaker driver
log "Checking the ReSpeaker 2-Mic HAT"
if ! aplay -l 2>/dev/null | grep -qi 'seeed\|wm8960\|tlv320'; then
    cat <<'EOF'
  The ReSpeaker HAT was not detected.

  On Bookworm and later the driver is a device-tree overlay. Add this to
  /boot/firmware/config.txt and reboot:

      dtoverlay=seeed-2mic-voicecard

  Then re-run this installer. Everything else will still be set up now.
EOF
else
    ok "ReSpeaker detected"
fi

# ------------------------------------------------------------------- user
log "Creating the service user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$DATA_DIR" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
fi
for group in audio i2c gpio; do
    getent group "$group" >/dev/null && usermod -aG "$group" "$SERVICE_USER"
done
ok "user '$SERVICE_USER' ready"

# -------------------------------------------------------------- signal-cli
log "Installing signal-cli $SIGNAL_CLI_VERSION"
if [[ ! -x /usr/local/bin/signal-cli ]] || \
   ! /usr/local/bin/signal-cli --version 2>/dev/null | grep -q "$SIGNAL_CLI_VERSION"; then
    tmp="$(mktemp -d)"
    url="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
    curl -fsSL "$url" -o "$tmp/signal-cli.tar.gz" \
        || die "could not download signal-cli $SIGNAL_CLI_VERSION"
    tar -xzf "$tmp/signal-cli.tar.gz" -C /opt
    ln -sf "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli
    rm -rf "$tmp"
fi
# --voice-note landed in 0.14.2; without it we cannot send real voice notes.
ok "signal-cli $(/usr/local/bin/signal-cli --version 2>/dev/null || echo '?')"

# ------------------------------------------------------------------- code
log "Installing Little Voicemail"
if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --all --prune
    git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
elif [[ -f "$(dirname "$0")/src/main.py" ]]; then
    mkdir -p "$INSTALL_DIR"
    cp -r "$(dirname "$0")/." "$INSTALL_DIR/"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
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
chmod 640 "$CONFIG_DIR/signal.env"
ok "directories ready"

# ----------------------------------------------------------------- reboot
log "Allowing the web UI to reboot for updates"
cat > /etc/sudoers.d/little-voicemail <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: /sbin/reboot
EOF
chmod 440 /etc/sudoers.d/little-voicemail
ok "sudoers rule installed"

# --------------------------------------------------------------- services
log "Installing systemd services"
cp "$INSTALL_DIR"/services/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable little-voicemail-web.service >/dev/null
ok "services installed"

# The phone and signal-cli services need an account before they can start,
# so only the web UI comes up now.
systemctl restart little-voicemail-web.service

hostname_short="$(hostname -s)"
address="$(hostname -I | awk '{print $1}')"

cat <<EOF

  Little Voicemail is installed.

  1. Open the parent settings and choose a password:

         https://${hostname_short}.local     (or https://${address})

     The certificate is self-signed, so your browser will warn you once.

  2. Link the device to a Signal account. On the Pi, run:

         sudo -u $SERVICE_USER signal-cli \\
             --config $DATA_DIR/signal-cli link -n "Little Voicemail"

     Turn the sgnl:// URI it prints into a QR code, then scan it from
     Signal on the phone that owns the number:
         Settings -> Linked devices -> Link new device

  3. Put that phone number into $CONFIG_DIR/signal.env and the web UI,
     then start the rest:

         sudo systemctl enable --now signal-cli little-voicemail

  Full walkthrough: $INSTALL_DIR/SETUP.md

EOF
