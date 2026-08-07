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
# Which audio overlay the board needs. Default is the ReSpeaker 2-Mics Pi HAT
# v2.0 (TLV320AIC3104), which is what HARDWARE.md specifies. For a v1.0 board
# (WM8960 codec) use LV_AUDIO_OVERLAY=wm8960-soundcard, which is in-tree and
# needs no compiling.
AUDIO_OVERLAY="${LV_AUDIO_OVERLAY:-respeaker-2mic-v2_0}"
SERVICE_USER="voicemail"
SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.7}"
# signal-cli 0.14.0 raised its floor to Java 25, and 0.14.2 is the first
# release with --voice-note, so there is no combination that runs on an
# older JRE. Debian stable does not carry 25 yet, hence install_java below.
JAVA_MIN=25
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
    git curl ca-certificates gnupg ffmpeg alsa-utils \
    i2c-tools libasound2-dev device-tree-compiler zip unzip \
    avahi-daemon \
    network-manager
ok "packages installed"

# ------------------------------------------------------------------- java
# signal-cli needs a JRE of at least $JAVA_MIN. Debian stable is behind - as
# of Bookworm the newest OpenJDK in the archive is 17 - so this tries the
# distro first and falls back rather than pinning a package name that only
# exists on some releases. Whatever happens, the version is checked at the
# end: a too-old JRE would install cleanly here and then fail with
# UnsupportedClassVersionError the first time a parent tried to link an
# account, on a box with no screen to show it on.

java_major() {
    local out
    # Match the version line rather than taking the first: with
    # JAVA_TOOL_OPTIONS or _JAVA_OPTIONS set - common in CI images and
    # containers - the JVM prints "Picked up ..." ahead of it, and parsing
    # that instead makes a perfectly good JRE look unusable.
    out="$(java -version 2>&1 | grep -m1 'version "')" || return 1
    # openjdk version "25.0.1" 2025-10-21  ->  25
    out="${out#*\"}"
    out="${out%%\"*}"
    out="${out%%.*}"
    [[ "$out" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$out"
}

java_is_new_enough() {
    local major
    major="$(java_major 2>/dev/null)" || return 1
    [[ "$major" -ge "$JAVA_MIN" ]]
}

try_apt_java() {
    local package="$1"
    apt-get install -y -qq "$package" >/dev/null 2>&1 || return 1
    java_is_new_enough
}

add_adoptium_repo() {
    local codename
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
    [[ -n "$codename" ]] || return 1
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL --retry 3 https://packages.adoptium.net/artifactory/api/gpg/key/public \
        | gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg 2>/dev/null || return 1
    chmod 0644 /etc/apt/keyrings/adoptium.gpg
    echo "deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb ${codename} main" \
        > /etc/apt/sources.list.d/adoptium.list
    apt-get update -qq 2>/dev/null || return 1
}

install_temurin_tarball() {
    # Last resort, and the same shape as the signal-cli install below: a
    # tarball into /opt with a symlink. No apt repo, no key, works on any
    # Debian. The trade is that it will not get security updates with the
    # rest of the system.
    local arch tmp url target
    case "$(uname -m)" in
        aarch64) arch="aarch64" ;;
        armv7l)  arch="arm" ;;
        x86_64)  arch="x64" ;;
        *)       return 1 ;;
    esac
    target="/opt/temurin-${JAVA_MIN}-jre"
    if [[ ! -x "$target/bin/java" ]]; then
        tmp="$(mktemp -d)"
        if [[ -n "${LV_JRE_TARBALL:-}" && -f "${LV_JRE_TARBALL}" ]]; then
            cp "$LV_JRE_TARBALL" "$tmp/jre.tar.gz"
        else
            url="https://api.adoptium.net/v3/binary/latest/${JAVA_MIN}/ga/linux/${arch}/jre/hotspot/normal/eclipse"
            curl -fsSL --retry 3 -o "$tmp/jre.tar.gz" "$url" || { rm -rf "$tmp"; return 1; }
        fi
        mkdir -p "$tmp/x"
        tar -xzf "$tmp/jre.tar.gz" -C "$tmp/x" || { rm -rf "$tmp"; return 1; }
        rm -rf "$target"
        mv "$tmp/x"/* "$target" || { rm -rf "$tmp"; return 1; }
        rm -rf "$tmp"
    fi
    # update-alternatives so `java` resolves for signal-cli's launcher and
    # for anyone debugging over SSH.
    update-alternatives --install /usr/bin/java java "$target/bin/java" 2000 >/dev/null
    update-alternatives --set java "$target/bin/java" >/dev/null 2>&1 || true
    java_is_new_enough
}

log "Installing a Java $JAVA_MIN runtime for signal-cli"
if java_is_new_enough; then
    ok "java $(java_major) already installed"
elif try_apt_java "openjdk-${JAVA_MIN}-jre-headless"; then
    ok "openjdk-${JAVA_MIN}-jre-headless from the distribution"
elif add_adoptium_repo && try_apt_java "temurin-${JAVA_MIN}-jre"; then
    ok "temurin-${JAVA_MIN}-jre from Adoptium"
elif install_temurin_tarball; then
    ok "Temurin $JAVA_MIN unpacked to /opt (no automatic security updates)"
else
    die "could not install a Java $JAVA_MIN runtime, which signal-cli requires.
     Tried openjdk-${JAVA_MIN}-jre-headless, Adoptium's temurin-${JAVA_MIN}-jre
     and the Temurin tarball. Install a JRE $JAVA_MIN+ by hand and re-run."
fi
java_is_new_enough \
    || die "java reports version $(java_major || echo unknown), but signal-cli needs $JAVA_MIN+"
ok "java $(java_major)"

# ------------------------------------------------------- respeaker overlay
# The codec needs a device-tree overlay, and which one depends on the board:
#
#   v2.0 (TLV320AIC3104)  respeaker-2mic-v2_0  -- compiled from the copy in
#                         tools/image/, because it does NOT ship with the OS
#   v1.0 (WM8960)         wm8960-soundcard     -- in-tree, ships with the OS
#
# `seeed-2mic-voicecard`, which older guides (including this project's own
# SETUP.md) name, is neither: it belongs to Seeed's out-of-tree DKMS driver,
# which broke after kernel 5.10 and which Seeed themselves no longer
# recommend. Writing it into config.txt gets you a Pi that boots with no
# sound card and no clue why.
log "Installing the audio overlay ($AUDIO_OVERLAY)"
overlay_dir="$(dirname "$BOOT_CONFIG")/overlays"
if [[ -f "$overlay_dir/$AUDIO_OVERLAY.dtbo" ]]; then
    ok "$AUDIO_OVERLAY ships with this image"
elif [[ -f "$SOURCE_DIR/tools/image/$AUDIO_OVERLAY-overlay.dts" ]]; then
    # -@ is required: the overlay targets &i2c1 and &sound by phandle, which
    # needs the symbol table.
    dtc -@ -I dts -O dtb -o "$overlay_dir/$AUDIO_OVERLAY.dtbo" \
        "$SOURCE_DIR/tools/image/$AUDIO_OVERLAY-overlay.dts" 2>/dev/null \
        || die "could not compile $AUDIO_OVERLAY-overlay.dts"
    ok "compiled $AUDIO_OVERLAY.dtbo"
else
    die "no overlay '$AUDIO_OVERLAY': not in $overlay_dir, and no
     tools/image/$AUDIO_OVERLAY-overlay.dts to compile. Set LV_AUDIO_OVERLAY
     to a board that exists - wm8960-soundcard for a ReSpeaker v1.0."
fi

# --------------------------------------------------------- boot config.txt
# Device-tree settings, so they only take effect after a reboot, and nothing
# works before they do: the codec for audio, I2C for the buttons and lights.
log "Configuring $BOOT_CONFIG"
if [[ -f "$BOOT_CONFIG" ]]; then
    grep -q '^dtparam=i2c_arm=on' "$BOOT_CONFIG" \
        || echo 'dtparam=i2c_arm=on' >> "$BOOT_CONFIG"
    # Earlier versions of this installer wrote an overlay that does not
    # exist. Take it out, so re-running repairs a Pi rather than leaving a
    # line that silently does nothing.
    if grep -q '^dtoverlay=seeed-2mic-voicecard' "$BOOT_CONFIG"; then
        sed -i '/^dtoverlay=seeed-2mic-voicecard/d' "$BOOT_CONFIG"
        log "  removed the stale dtoverlay=seeed-2mic-voicecard line"
    fi
    grep -q "^dtoverlay=$AUDIO_OVERLAY" "$BOOT_CONFIG" \
        || echo "dtoverlay=$AUDIO_OVERLAY" >> "$BOOT_CONFIG"
    ok "i2c and $AUDIO_OVERLAY are enabled in $BOOT_CONFIG"
else
    log "  no $BOOT_CONFIG (not a Raspberry Pi?); skipping"
fi
grep -q '^i2c-dev' /etc/modules 2>/dev/null || echo 'i2c-dev' >> /etc/modules

if ! image_build && command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0 || true
fi

if ! image_build; then
    log "Checking the ReSpeaker HAT"
    if aplay -l 2>/dev/null | grep -qi 'seeed\|wm8960\|tlv320'; then
        ok "sound card detected"
    else
        echo "  Not detected yet - the overlay has only just been enabled."
        echo "  Reboot, then check with: aplay -l"
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

# ---------------------------------------------------- libsignal native lib
# signal-cli is mostly Java, but the Signal protocol itself is a Rust library
# loaded through JNI - and the release tarball bundles that library for
# x86_64 Linux, Windows and macOS *only*. Its own README says so. On a
# Raspberry Pi there is simply no native library in the jar, so signal-cli
# fails to start: no linking, no messages, nothing.
#
# The fix is the one signal-cli's wiki documents: drop the wrong-architecture
# binaries and splice in one built for this machine. exquo/signal-libs-build
# publishes them from GitHub Actions, versioned to match libsignal exactly.
#
# Doing this also takes the jar from ~65 MB to ~10 MB, because the bundle we
# are replacing includes a 182 MB x86-64 shared object.
patch_libsignal() {
    local jar libsignal_version triple tmp url

    jar="$(find "/opt/signal-cli-${SIGNAL_CLI_VERSION}/lib" \
           -name 'libsignal-client-*.jar' -print -quit 2>/dev/null)"
    [[ -n "$jar" ]] || die "no libsignal-client jar in the signal-cli install"

    # Already patched by a previous run?
    if unzip -l "$jar" 2>/dev/null | grep -qE ' libsignal_jni\.so$'; then
        ok "libsignal native library already in place"
        return 0
    fi

    # Take the version from the jar, so this keeps working across signal-cli
    # bumps without a second version constant to forget to update.
    libsignal_version="$(basename "$jar")"
    libsignal_version="${libsignal_version#libsignal-client-}"
    libsignal_version="${libsignal_version%.jar}"

    case "$(uname -m)" in
        aarch64) triple="aarch64-unknown-linux-gnu" ;;
        armv7l)  triple="armv7-unknown-linux-gnueabihf" ;;
        *)       die "no prebuilt libsignal for $(uname -m); see
     https://github.com/AsamK/signal-cli/wiki/Provide-native-lib-for-libsignal" ;;
    esac

    log "Fetching libsignal $libsignal_version for $triple"
    tmp="$(mktemp -d)"
    if [[ -n "${LV_LIBSIGNAL_TARBALL:-}" && -f "${LV_LIBSIGNAL_TARBALL}" ]]; then
        cp "$LV_LIBSIGNAL_TARBALL" "$tmp/lib.tar.gz"
    else
        url="https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${libsignal_version}/libsignal_jni.so-v${libsignal_version}-${triple}.tar.gz"
        curl -fsSL --retry 3 -o "$tmp/lib.tar.gz" "$url" || {
            rm -rf "$tmp"
            die "could not download libsignal $libsignal_version for $triple.
     signal-cli cannot run on this architecture without it. Build it yourself
     per https://github.com/AsamK/signal-cli/wiki/Provide-native-lib-for-libsignal
     and re-run with LV_LIBSIGNAL_TARBALL pointing at the .tar.gz."
        }
    fi
    tar -xzf "$tmp/lib.tar.gz" -C "$tmp" || { rm -rf "$tmp"; die "bad libsignal tarball"; }
    [[ -f "$tmp/libsignal_jni.so" ]] \
        || { rm -rf "$tmp"; die "no libsignal_jni.so in the tarball"; }

    # Exactly the procedure from signal-cli's wiki: remove every bundled
    # native, then add ours under the unqualified name the loader falls
    # back to.
    zip -q "$jar" -d '*signal_jni*' 2>/dev/null || true
    ( cd "$tmp" && zip -quj "$jar" libsignal_jni.so ) \
        || { rm -rf "$tmp"; die "could not splice libsignal into $jar"; }
    rm -rf "$tmp"
    ok "libsignal $libsignal_version ($triple) spliced in"
}
patch_libsignal

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
# anything, so only the web UI, the setup portal and the audio-levels
# one-shot come up now. The Signal tab in the web UI enables the other two
# once linking succeeds. audio-levels doesn't need an account - it just
# maxes out the codec's playback controls - but on a brand new install it
# runs before the reboot that brings the overlay up, so it no-ops the first
# time and does its actual job on every boot after that (see the service's
# WantedBy=multi-user.target).
enable_now=(
    little-voicemail-web.service
    little-voicemail-portal.service
    little-voicemail-audio-levels.service
)

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
