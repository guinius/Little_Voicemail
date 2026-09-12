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


# -- the local certificate authority (GitHub issue #18) ---------------------


def test_a_ca_is_generated_on_first_start(certs):
    ca_cert, ca_key = server.ensure_ca()
    assert ca_cert.exists() and ca_key.exists()
    assert ca_key.stat().st_mode & 0o777 == 0o600
    assert b"BEGIN CERTIFICATE" in ca_cert.read_bytes()


def test_the_same_ca_is_reused(certs):
    first = server.ensure_ca()[0].read_bytes()
    second = server.ensure_ca()[0].read_bytes()
    assert first == second


def test_an_unreadable_ca_is_replaced_not_fatal(certs):
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "ca.crt").write_text("nope")
    (certs / "ca.key").write_text("nope either")

    ca_cert, _ = server.ensure_ca()
    assert b"BEGIN CERTIFICATE" in ca_cert.read_bytes()


def test_the_ca_is_actually_a_ca(certs):
    """BasicConstraints CA:TRUE - the thing that makes installing it as a
    trusted root actually work, rather than just looking like a certificate."""
    from cryptography import x509

    ca_cert_path, _ = server.ensure_ca()
    cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert constraints.ca is True


def test_the_server_certificate_is_signed_by_the_device_ca(certs, monkeypatch):
    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")

    ca_cert_path, _ = server.ensure_ca()
    cert_path, _ = server.ensure_certificate("littlevoicemail")

    assert server._leaf_signed_by_ca(cert_path, ca_cert_path)


def test_a_pre_ca_self_signed_certificate_is_replaced(certs, monkeypatch):
    """A certificate from before this device had a CA (self-signed, issuer
    == subject) must not be kept just because its names still match - it
    was never signed by the CA a parent would go on to install."""
    monkeypatch.setattr(server, "local_ip", lambda: "192.168.1.50")
    dns_names, addresses = server.certificate_names("littlevoicemail")

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as dt

    certs.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "littlevoicemail")])
    now = dt.datetime.now(dt.timezone.utc)
    alt_names = [x509.DNSName(n) for n in dns_names]
    legacy = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(key, hashes.SHA256())
    )
    (certs / "server.crt").write_bytes(legacy.public_bytes(serialization.Encoding.PEM))
    (certs / "server.key").write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    cert_path, _ = server.ensure_certificate("littlevoicemail")
    ca_cert_path = certs / "ca.crt"
    assert server._leaf_signed_by_ca(cert_path, ca_cert_path)
