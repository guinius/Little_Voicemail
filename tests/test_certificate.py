"""The self-signed certificate.

The box is reached by whatever name or address it happens to have, and that
changes: Raspberry Pi Imager can set a hostname the config file never heard
of, DHCP hands out a different lease, or the very first start happened while
the setup hotspot was up and 10.42.0.1 was the only address there was. A
certificate minted once and kept forever stops matching, and the parent gets
a warning that looks exactly like the one they are told to ignore.
"""

import pytest

from src.web import server


@pytest.fixture
def certs(tmp_path, monkeypatch):
    directory = tmp_path / "certs"
    monkeypatch.setattr(server, "certs_dir", lambda: directory)
    return directory


def test_a_certificate_is_generated_on_first_start(certs, monkeypatch):
    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")
    cert_path, key_path = server.ensure_certificate("littlevoicemail")
    assert cert_path.exists() and key_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_the_same_certificate_is_reused(certs, monkeypatch):
    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")
    first = server.ensure_certificate("littlevoicemail")[0].read_bytes()
    second = server.ensure_certificate("littlevoicemail")[0].read_bytes()
    assert first == second


def test_a_new_address_forces_a_new_certificate(certs, monkeypatch):
    """A first start on the setup hotspot must not poison the real address."""
    monkeypatch.setattr(server, "local_ip", lambda: "10.42.0.1")
    on_hotspot = server.ensure_certificate("littlevoicemail")[0].read_bytes()

    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")
    on_lan = server.ensure_certificate("littlevoicemail")[0].read_bytes()

    assert on_lan != on_hotspot
    assert server._certificate_covers(
        certs / "server.crt",
        *server.certificate_names("littlevoicemail"),
    )


def test_a_new_hostname_forces_a_new_certificate(certs, monkeypatch):
    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")
    original = server.ensure_certificate("littlevoicemail")[0].read_bytes()
    renamed = server.ensure_certificate("hallway-phone")[0].read_bytes()
    assert renamed != original


def test_the_names_cover_both_hostnames_and_mdns(monkeypatch):
    monkeypatch.setattr(server.socket, "gethostname", lambda: "hallway-phone")
    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")
    dns, addresses = server.certificate_names("littlevoicemail")
    # Whichever name the parent was told about has to work.
    assert "littlevoicemail" in dns
    assert "littlevoicemail.local" in dns
    assert "hallway-phone" in dns
    assert "hallway-phone.local" in dns
    assert "localhost" in dns
    assert addresses == ["127.0.0.1", "192.168.1.50"]


def test_an_unreadable_certificate_is_replaced_not_fatal(certs, monkeypatch):
    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "server.crt").write_text("this is not a certificate")
    (certs / "server.key").write_text("nor is this")

    cert_path, _ = server.ensure_certificate("littlevoicemail")
    assert b"BEGIN CERTIFICATE" in cert_path.read_bytes()
