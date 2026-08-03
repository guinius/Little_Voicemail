"""The WiFi setup portal.

lv-netctl is stubbed throughout: the point of these is the decision-making
around the hotspot, not nmcli itself.
"""

import time

import pytest

from src.config import Config
from src.web.portal import NetControl, SetupPortal, create_portal_app


class FakeNet(NetControl):
    """Stands in for the lv-netctl helper."""

    def __init__(self, online=False):
        self.calls: list[list[str]] = []
        self.online = online
        self.hotspot = False
        self.join_succeeds = True
        super().__init__(runner=self._runner)

    def _runner(self, command):
        self.calls.append(list(command))
        verb = command[3]
        if verb == "status":
            return 0, (
                f"state={'connected' if self.online else 'disconnected'}\n"
                f"ssid={'home' if self.online else ''}\n"
                f"hotspot={'yes' if self.hotspot else 'no'}\n"
                "address=10.42.0.1\n"
            )
        if verb == "scan":
            return 0, "Home WiFi:71:WPA2\nNeighbour:41:WPA2\nOpen Cafe:22:\n"
        if verb == "hotspot-up":
            self.hotspot = True
            return 0, ""
        if verb == "hotspot-down":
            self.hotspot = False
            return 0, ""
        if verb == "join":
            if not self.join_succeeds:
                return 1, "could not join"
            self.online = True
            self.hotspot = False
            return 0, ""
        return 1, "unknown verb"


@pytest.fixture
def portal(tmp_path):
    return SetupPortal(Config(tmp_path / "config.json"), net=FakeNet())


@pytest.fixture
def client(portal):
    app = create_portal_app(portal._config, portal)
    app.config.update(TESTING=True)
    return app.test_client()


# -- deciding whether the hotspot is needed --------------------------------


def test_no_network_raises_the_hotspot(portal):
    portal.tick()
    assert portal.net.hotspot is True
    assert ["sudo", "-n", portal.net.helper, "hotspot-up",
            "Little Voicemail setup", "voicemail"] in portal.net.calls


def test_a_working_network_leaves_the_hotspot_alone(portal):
    portal.net.online = True
    portal.tick()
    assert portal.net.hotspot is False
    assert not any("hotspot-up" in call for call in portal.net.calls)


def test_the_hotspot_comes_down_once_the_network_is_up(portal):
    portal.tick()
    assert portal.net.hotspot is True
    portal.net.online = True
    portal.tick()
    assert portal.net.hotspot is False


def test_the_hotspot_can_be_turned_off_in_config(portal):
    portal._config.set(False, "network", "setup_ap", "enabled")
    portal.tick()
    assert portal.net.hotspot is False


def test_networks_are_scanned_before_the_radio_goes_into_ap_mode(portal):
    """The Pi's WiFi chip cannot scan and be an access point at once."""
    portal.tick()
    verbs = [call[3] for call in portal.net.calls]
    assert verbs.index("scan") < verbs.index("hotspot-up")
    assert [n["ssid"] for n in portal.networks] == [
        "Home WiFi", "Neighbour", "Open Cafe"
    ]
    assert portal.networks[2]["secure"] is False


# -- the page --------------------------------------------------------------


def test_the_setup_page_lists_what_it_found(portal, client):
    portal.tick()
    body = client.get("/").data
    assert b"Choose your WiFi" in body
    assert b"Home WiFi" in body


def test_an_online_box_redirects_to_the_https_ui(portal, client):
    portal.online = True
    response = client.get("/")
    assert response.status_code == 301
    assert response.headers["Location"].startswith("https://")
    assert ":8443" in response.headers["Location"]


def test_captive_portal_probes_are_answered(portal, client):
    portal.tick()
    for path in ("/generate_204", "/hotspot-detect.html", "/ncsi.txt"):
        response = client.get(path)
        assert response.status_code == 302, path
        assert "10.42.0.1" in response.headers["Location"], path


def test_unknown_paths_lead_back_to_setup(portal, client):
    portal.tick()
    response = client.get("/something/else")
    assert response.status_code == 302
    assert "10.42.0.1" in response.headers["Location"]


# -- joining ---------------------------------------------------------------


def test_joining_answers_before_the_network_disappears(portal, client):
    portal.tick()
    response = client.post("/join", data={"ssid": "Home WiFi", "password": "hunter22"})
    assert response.status_code == 200
    assert b"about to disappear" in response.data
    _settle(portal)
    assert portal.net.online is True


def test_a_hidden_network_is_flagged_to_the_helper(portal, client):
    portal.tick()
    client.post(
        "/join",
        data={"ssid": "Invisible", "password": "hunter22", "hidden": "on"},
    )
    _settle(portal)
    assert ["sudo", "-n", portal.net.helper, "join", "Invisible", "hunter22",
            "--hidden"] in portal.net.calls


def test_an_open_network_needs_no_password(portal, client):
    portal.tick()
    client.post("/join", data={"ssid": "Open Cafe", "password": ""})
    _settle(portal)
    assert ["sudo", "-n", portal.net.helper, "join", "Open Cafe", ""] \
        in portal.net.calls


def test_a_short_password_is_refused_before_the_network_drops(portal, client):
    portal.tick()
    response = client.post("/join", data={"ssid": "Home WiFi", "password": "short"})
    assert response.status_code == 400
    assert b"at least 8 characters" in response.data
    assert not any("join" == call[3] for call in portal.net.calls)


def test_a_missing_ssid_is_refused(portal, client):
    portal.tick()
    response = client.post("/join", data={"ssid": "  ", "password": "hunter22"})
    assert response.status_code == 400
    assert not any("join" == call[3] for call in portal.net.calls)


def test_an_ssid_that_looks_like_an_option_is_refused(portal, client):
    """Everything here ends up as an nmcli argument."""
    portal.tick()
    response = client.post("/join", data={"ssid": "-delete", "password": "hunter22"})
    assert response.status_code == 400
    assert not any("join" == call[3] for call in portal.net.calls)


def test_a_wrong_password_brings_the_hotspot_back(portal, client):
    portal.tick()
    portal.net.join_succeeds = False
    client.post("/join", data={"ssid": "Home WiFi", "password": "wrongwrong"})
    _settle(portal)
    # A mistyped password must be recoverable without a reflash.
    assert portal.net.hotspot is True
    assert "Could not join" in portal.last_error
    assert b"Could not join" in client.get("/").data


def _settle(portal, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not portal.joining:
            return
        time.sleep(0.05)
    raise AssertionError("the join never finished")
