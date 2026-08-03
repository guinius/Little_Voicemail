"""WiFi setup portal.

The box has no screen and no keyboard. If it boots without a network - the
WiFi details were mistyped in Raspberry Pi Imager, or the router was
replaced, or it moved house - there is no way in at all: no mDNS name to
open, no SSH to fall back to, nothing but a reflash.

So when there is no network after a grace period, the Pi raises its own
access point and serves this page on it. A phone joins that network, gets
the captive-portal prompt, picks the home WiFi and types the password; the
Pi then drops its own network and joins that one.

The same process owns port 80 the rest of the time, where it does nothing
but redirect to the HTTPS parent UI - one owner for the port, so the two
services never race for the bind.

Everything privileged goes through `lv-netctl`, a root helper with a fixed
verb set, rather than a blanket sudo grant on nmcli.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

from ..config import Config
from ..paths import default_config_path

log = logging.getLogger("little_voicemail.portal")

NETCTL = "/usr/local/lib/little-voicemail/lv-netctl"
HOTSPOT_ADDRESS = "10.42.0.1"  # NetworkManager's shared-mode gateway

DEFAULT_SSID = "Little Voicemail setup"
DEFAULT_PASSWORD = "voicemail"
DEFAULT_GRACE = 60

# The URLs phones and laptops fetch to decide whether a network is captive.
# Answering them with a redirect is what makes the "Sign in to network" sheet
# appear on its own, rather than the parent having to know to type an address.
PROBE_PATHS = (
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/ncsi.txt",
    "/connecttest.txt",
    "/canonical.html",
    "/success.txt",
)

CHECK_INTERVAL = 10
JOIN_SETTLE = 45


class NetControl:
    """Thin wrapper around the lv-netctl root helper."""

    def __init__(self, helper: str = NETCTL, runner=None):
        self.helper = helper
        self._runner = runner or _run

    def _call(self, *args: str) -> tuple[bool, str]:
        code, out = self._runner(["sudo", "-n", self.helper, *args])
        return code == 0, out

    def status(self) -> dict:
        ok, out = self._call("status")
        parsed: dict[str, str] = {}
        if ok:
            for line in out.splitlines():
                key, _, value = line.partition("=")
                parsed[key.strip()] = value.strip()
        return {
            "online": parsed.get("state", "") == "connected",
            "ssid": parsed.get("ssid", ""),
            "hotspot": parsed.get("hotspot", "") == "yes",
            "address": parsed.get("address", ""),
        }

    def scan(self) -> list[dict]:
        ok, out = self._call("scan")
        networks = []
        if not ok:
            return networks
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 2 or not parts[0]:
                continue
            try:
                strength = int(parts[1])
            except ValueError:
                strength = 0
            networks.append(
                {
                    "ssid": parts[0],
                    "strength": strength,
                    "secure": bool(":".join(parts[2:]).strip()),
                }
            )
        return networks[:20]

    def hotspot_up(self, ssid: str, password: str) -> tuple[bool, str]:
        return self._call("hotspot-up", ssid, password)

    def hotspot_down(self) -> tuple[bool, str]:
        return self._call("hotspot-down")

    def join(self, ssid: str, password: str, hidden: bool) -> tuple[bool, str]:
        args = ["join", ssid, password]
        if hidden:
            args.append("--hidden")
        return self._call(*args)


class SetupPortal:
    """Decides whether the hotspot should be up, and joins networks."""

    def __init__(self, config: Config, net: NetControl | None = None):
        self._config = config
        self.net = net or NetControl()
        self._lock = threading.RLock()
        self.networks: list[dict] = []
        self.last_error = ""
        self.joining = ""
        self.online = False

    # -- settings --------------------------------------------------------

    @property
    def ap_ssid(self) -> str:
        return self._config.get(
            "network", "setup_ap", "ssid", default=DEFAULT_SSID
        )

    @property
    def ap_password(self) -> str:
        return self._config.get(
            "network", "setup_ap", "password", default=DEFAULT_PASSWORD
        )

    @property
    def grace_seconds(self) -> int:
        return int(
            self._config.get(
                "network", "setup_ap", "grace_seconds", default=DEFAULT_GRACE
            )
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self._config.get("network", "setup_ap", "enabled", default=True)
        )

    @property
    def https_port(self) -> int:
        return int(self._config.get("web", "port", default=8443))

    # -- the loop --------------------------------------------------------

    def wait_for_network(self) -> bool:
        """Give a normal boot a chance before concluding there is no network."""
        deadline = time.time() + self.grace_seconds
        while time.time() < deadline:
            if self.net.status()["online"]:
                return True
            time.sleep(2)
        return self.net.status()["online"]

    def run(self) -> None:
        online = self.wait_for_network()
        with self._lock:
            self.online = online
        if online:
            log.info("network is up; the setup hotspot is not needed")
        while True:
            try:
                self.tick()
            except Exception:  # a monitor thread must not be the thing that dies
                log.exception("portal monitor failed; continuing")
            time.sleep(CHECK_INTERVAL)

    def tick(self) -> None:
        with self._lock:
            if self.joining:
                return
        status = self.net.status()
        with self._lock:
            self.online = status["online"]
        if status["online"]:
            if status["hotspot"]:
                log.info("network is up; taking the setup hotspot down")
                self.net.hotspot_down()
            return
        if not self.enabled or status["hotspot"]:
            return
        self.raise_hotspot()

    def raise_hotspot(self) -> None:
        # Scan before the radio goes into AP mode: the Pi's WiFi chip cannot
        # do both at once, so this is the only chance to see what is around.
        networks = self.net.scan()
        with self._lock:
            if networks:
                self.networks = networks
        log.info("no network; raising '%s'", self.ap_ssid)
        ok, detail = self.net.hotspot_up(self.ap_ssid, self.ap_password)
        if not ok:
            log.error("could not raise the setup hotspot: %s", detail.strip())

    # -- joining ---------------------------------------------------------

    def start_join(self, ssid: str, password: str, hidden: bool) -> None:
        """Join in the background; the browser is about to lose this network."""
        with self._lock:
            self.joining = ssid
            self.last_error = ""
        threading.Thread(
            target=self._join, args=(ssid, password, hidden),
            daemon=True, name="portal-join",
        ).start()

    def _join(self, ssid: str, password: str, hidden: bool) -> None:
        try:
            ok, detail = self.net.join(ssid, password, hidden)
            if ok:
                deadline = time.time() + JOIN_SETTLE
                while time.time() < deadline:
                    if self.net.status()["online"]:
                        log.info("joined '%s'", ssid)
                        with self._lock:
                            self.online = True
                        return
                    time.sleep(2)
                detail = "joined the network but it never came up"
            log.warning("could not join '%s': %s", ssid, detail.strip())
            with self._lock:
                self.last_error = (
                    f"Could not join '{ssid}'. Check the password and try again."
                )
            # Come back so the parent gets another go without a reflash.
            self.raise_hotspot()
        finally:
            with self._lock:
                self.joining = ""


def create_portal_app(config: Config, portal: SetupPortal) -> Flask:
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=64 * 1024)

    def setup_needed() -> bool:
        with portal._lock:
            return not portal.online or bool(portal.joining)

    def render_setup(**extra):
        with portal._lock:
            context = {
                "networks": list(portal.networks),
                "error": portal.last_error,
                "joining": portal.joining,
                "ap_ssid": portal.ap_ssid,
                "https_port": portal.https_port,
                "hostname": config.get("web", "hostname", default="littlevoicemail"),
            }
        context.update(extra)
        return render_template("portal_wifi.html", **context)

    def bounce_to_https():
        host = (request.headers.get("Host") or "").split(":")[0]
        return redirect(f"https://{host}:{portal.https_port}/", code=301)

    @app.route("/", methods=["GET"])
    def index():
        if not setup_needed():
            return bounce_to_https()
        return render_setup()

    @app.route("/join", methods=["POST"])
    def join():
        ssid = (request.form.get("ssid") or "").strip()
        password = request.form.get("password") or ""
        hidden = request.form.get("hidden") == "on"

        if not ssid:
            return render_setup(error="Choose a network, or type its name."), 400
        if ssid.startswith("-"):
            return render_setup(error="That network name cannot be used."), 400
        if password and len(password) < 8:
            return render_setup(
                error="A WiFi password is at least 8 characters."
            ), 400

        portal.start_join(ssid, password, hidden)
        # Answer now: joining tears down the network this request arrived on.
        return render_template(
            "portal_joining.html",
            ssid=ssid,
            hostname=config.get("web", "hostname", default="littlevoicemail"),
            https_port=portal.https_port,
        )

    @app.route("/rescan", methods=["POST"])
    def rescan():
        # Scanning needs the radio out of AP mode, so this drops the hotspot,
        # looks around, and puts it back. The browser reconnects on its own.
        portal.net.hotspot_down()
        portal.raise_hotspot()
        return redirect(url_for("index"))

    for path in PROBE_PATHS:
        app.add_url_rule(
            path, f"probe{path.replace('/', '_').replace('.', '_')}",
            lambda: redirect(f"http://{HOTSPOT_ADDRESS}/", code=302),
        )

    @app.errorhandler(404)
    def catch_all(_error):
        """A captive portal answers everything, whatever was asked for."""
        if setup_needed():
            return redirect(f"http://{HOTSPOT_ADDRESS}/", code=302)
        return bounce_to_https()

    return app


def _run(command) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(command), capture_output=True, text=True, timeout=90
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    parser = argparse.ArgumentParser(prog="little-voicemail-portal")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config = Config(args.config)
    portal = SetupPortal(config)
    app = create_portal_app(config, portal)

    threading.Thread(target=portal.run, daemon=True, name="portal-monitor").start()

    from cheroot.wsgi import Server as WSGIServer

    server = WSGIServer(
        (args.host, args.port), app, numthreads=4, server_name="little-voicemail-portal"
    )
    log.info("setup portal listening on port %s", args.port)
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
