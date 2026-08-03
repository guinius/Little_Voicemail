#!/usr/bin/env bash
#
# Runs inside the chroot, last. Strips everything that must be unique to a
# device from what is about to become a file thousands of people could flash.
#
# The removals are the intent; `verify_clean` is the guarantee. If an
# assertion fails the build fails, because the alternative is shipping one
# shared TLS private key, or one shared session secret, to every box.
#
set -euo pipefail

DATA_DIR="/var/lib/little-voicemail"
CONFIG_DIR="/etc/little-voicemail"
log()  { printf '\033[1;33m  clean>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m  !! not clean:\033[0m %s\n' "$*" >&2; BAD=1; }

# -- identity ---------------------------------------------------------------
log "removing per-device identity"
# Zero-length, not deleted: that is systemd's documented "generate me on boot".
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id
rm -f /var/lib/systemd/random-seed /var/lib/systemd/credential.secret
rm -f /etc/ssh/ssh_host_*

# -- little voicemail state -------------------------------------------------
log "removing device state"
rm -f  "$DATA_DIR"/certs/* "$DATA_DIR"/session.key "$DATA_DIR"/status.json
rm -f  "$DATA_DIR"/messages.db*
rm -rf "$DATA_DIR"/recordings/* "$DATA_DIR"/signal-cli/* "$DATA_DIR"/signal-cli.unlinked-*
mkdir -p "$DATA_DIR"/{recordings,certs,signal-cli}
chmod 700 "$DATA_DIR/signal-cli"
echo 'SIGNAL_ACCOUNT=' > "$CONFIG_DIR/signal.env"
chmod 640 "$CONFIG_DIR/signal.env"

python3 - "$CONFIG_DIR/config.json" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path) as fh:
    config = json.load(fh)
config.setdefault("web", {})["password_hash"] = ""
config.setdefault("signal", {})["account"] = ""
config.setdefault("updates", {}).pop("token", None)
with open(path, "w") as fh:
    json.dump(config, fh, indent=2)
PY

chown -R voicemail:voicemail "$DATA_DIR" "$CONFIG_DIR"

# -- the builder's own traces ----------------------------------------------
log "removing build traces"
rm -f  /etc/NetworkManager/system-connections/* 2>/dev/null || true
rm -f  /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null || true
rm -rf /var/lib/apt/lists/*
apt-get clean
rm -f  /var/cache/apt/archives/*.deb
# /usr/local/src/lv-build holds this very script, so the builder removes it
# from outside once the chroot has been left. Deleting a running script out
# from under bash makes it misread whatever it had not buffered yet.
rm -f  /root/.bash_history /home/*/.bash_history 2>/dev/null || true
rm -rf /root/.cache /tmp/* 2>/dev/null || true
find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true

# -- the guarantee ----------------------------------------------------------
# Written as `if ... then fail; fi` rather than `test && fail`, because under
# `set -e` a bare `test && fail` aborts the script on the *passing* case.
BAD=0
log "checking nothing personal survived"

assert_empty_glob() {
    local pattern="$1" what="$2"
    if compgen -G "$pattern" >/dev/null 2>&1; then
        fail "$what"
    fi
}

if [[ -s /etc/machine-id ]]; then
    fail "/etc/machine-id is not empty"
fi
if [[ -e "$DATA_DIR/session.key" ]]; then
    fail "the Flask session key is present"
fi
assert_empty_glob "/etc/ssh/ssh_host_*" "SSH host keys are present"
assert_empty_glob "$DATA_DIR/certs/*" "a TLS certificate is present"
assert_empty_glob "$DATA_DIR/signal-cli/*" "Signal account state is present"
assert_empty_glob "/etc/NetworkManager/system-connections/*" \
    "the builder's WiFi credentials are present"

if ! grep -q '^SIGNAL_ACCOUNT=$' "$CONFIG_DIR/signal.env"; then
    fail "signal.env still names an account"
fi

if ! python3 - "$CONFIG_DIR/config.json" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1]))
problems = []
if config.get("web", {}).get("password_hash"):
    problems.append("config.json carries a password hash")
if config.get("signal", {}).get("account"):
    problems.append("config.json names a Signal account")
if config.get("updates", {}).get("token"):
    problems.append("config.json carries a GitHub token")
for problem in problems:
    print(f"  !! not clean: {problem}", file=sys.stderr)
sys.exit(1 if problems else 0)
PY
then
    BAD=1
fi

# A CI checkout stores its credential header in .git/config, and that would
# ship a live token inside every copy of the image.
if [[ -f /opt/little-voicemail/.git/config ]] \
   && grep -Eq 'extraheader|x-access-token|ghp_|github_pat_' \
        /opt/little-voicemail/.git/config; then
    fail "a git credential is baked into the checkout"
fi

hits="$(grep -rIl -E 'ghp_[A-Za-z0-9]{20}|github_pat_|BEGIN [A-Z ]*PRIVATE KEY' \
        "$CONFIG_DIR" "$DATA_DIR" 2>/dev/null || true)"
if [[ -n "$hits" ]]; then
    while read -r hit; do
        [[ -n "$hit" ]] && fail "secret-looking content in $hit"
    done <<< "$hits"
fi

if [[ "$BAD" != "0" ]]; then
    printf '\033[1;31m!! refusing to ship this image\033[0m\n' >&2
    exit 1
fi
log "clean"
