"""HTTPS entry point for the parent web UI.

Requirement 8 is "connect securely from a device on the same local network".
There is no public hostname to get a real certificate for, so the box mints
its own private certificate authority (CA) at first boot, unique to that
device, and signs its own server certificate with it - valid for its
hostname, its .local mDNS name and its LAN address. A browser that has never
seen this device's CA still warns once, exactly as it would for a plain
self-signed certificate; the difference is that a parent can install the CA
certificate itself (served at GET /ca.crt, with instructions on the
/certificate page) on their phone or computer once, after which every future
visit is fully trusted with no warning at all - the same padlock a public
website gets, because a local CA is exactly what a public CA is, just one
this box made for itself instead of buying from someone browsers already
trust (see GitHub issue #18).

Plain HTTP is not served at all - only a redirect listener that bounces to
the HTTPS port, so a mistyped http:// never sends the parent password in
the clear.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import logging
import os
import socket
import ssl
import sys
import threading
from pathlib import Path

from cheroot.ssl.builtin import BuiltinSSLAdapter
from cheroot.wsgi import Server as WSGIServer

from ..paths import certs_dir, default_config_path, default_data_dir, default_sounds_dir
from .app import create_app

log = logging.getLogger("little_voicemail.web")

CERT_VALID_DAYS = 3650
# Long-lived on purpose: reissuing the CA would silently untrust every
# device a parent has already installed the old one on, with no way to
# tell them short of the browser warning coming back. A device-specific
# private CA carries none of the reasons a *publicly* trusted CA keeps its
# lifetime short (mass revocation blast radius, browser policy) - it is
# trusted by exactly the devices one parent chose to trust it on.
CA_VALID_DAYS = 3650 * 3


def local_ip() -> str:
    """Best guess at this box's LAN address."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this just picks the interface with a route out.
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def certificate_names(hostname: str) -> tuple[list[str], list[str]]:
    """The DNS names and IP addresses the certificate has to cover.

    Both the configured hostname and the system one, because Raspberry Pi
    Imager can set a hostname the config file has never heard of, and the
    parent will type whichever one they were told about.
    """
    names = {hostname, socket.gethostname(), "littlevoicemail"}
    names = {n for n in names if n and n != "localhost"}
    dns = sorted(names) + sorted(f"{n}.local" for n in names) + ["localhost"]
    addresses = ["127.0.0.1"]
    address = local_ip()
    if address not in addresses:
        addresses.append(address)
    return dns, addresses


def _certificate_covers(cert_path: Path, dns: list[str], addresses: list[str]) -> bool:
    """True if an existing certificate still matches how the box is reached."""
    try:
        from cryptography import x509

        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        have_dns = set(san.get_values_for_type(x509.DNSName))
        have_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    except Exception:  # unreadable or SAN-less: treat as not covering
        return False
    return set(dns) <= have_dns and set(addresses) <= have_ips


def ensure_ca() -> tuple[Path, Path]:
    """Return (cert, key) for this device's own certificate authority,
    generating one the first time it's needed.

    One CA per device, made once and kept forever (see CA_VALID_DAYS) - a
    parent who installs it on a phone trusts *this box*, not every Little
    Voicemail device everywhere, which is exactly what a device-specific CA
    key (rather than one baked into the software and shared by every
    install) buys: installing it on your own phone couldn't accidentally
    trust someone else's box even if you wanted it to.
    """
    directory = certs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    ca_cert_path = directory / "ca.crt"
    ca_key_path = directory / "ca.key"

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if ca_cert_path.exists() and ca_key_path.exists():
        try:
            x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
            serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
            return ca_cert_path, ca_key_path
        except Exception:
            log.warning("this device's CA certificate is unreadable; making a new one")

    log.info("generating this device's own certificate authority")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Little Voicemail Local CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Little Voicemail"),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    ca_key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    ca_key_path.chmod(0o600)
    ca_cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return ca_cert_path, ca_key_path


def _leaf_signed_by_ca(cert_path: Path, ca_cert_path: Path) -> bool:
    """True if the certificate at `cert_path` was actually signed by the CA
    at `ca_cert_path` - not just issued by something with a matching name.

    Needed so a certificate minted before this device had a CA (an older
    self-signed one from before this feature existed) gets regenerated
    rather than kept just because it still covers the right names.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import padding

        leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        ca_cert.public_key().verify(
            leaf.signature, leaf.tbs_certificate_bytes,
            padding.PKCS1v15(), leaf.signature_hash_algorithm,
        )
        return True
    except Exception:
        return False


def ensure_certificate(hostname: str) -> tuple[Path, Path]:
    """Return (cert, key) for the server's own certificate, signed by this
    device's CA (see ensure_ca), generating one if needed.

    Regenerated when the box has become reachable by a name or address the
    existing certificate does not cover - a new DHCP lease, an Imager-set
    hostname, or a first start that happened while the setup hotspot was up
    and the only address was the hotspot's own - or when it wasn't actually
    signed by the current CA at all.
    """
    directory = certs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    ca_cert_path, ca_key_path = ensure_ca()
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    dns_names, addresses = certificate_names(hostname)
    if cert_path.exists() and key_path.exists():
        if _certificate_covers(cert_path, dns_names, addresses) and _leaf_signed_by_ca(
            cert_path, ca_cert_path
        ):
            return cert_path, key_path
        log.info("the certificate no longer matches this box or its CA; making a new one")

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    log.info("generating a certificate for %s, signed by this device's CA", hostname)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Little Voicemail"),
        ]
    )
    alt_names: list[x509.GeneralName] = [x509.DNSName(n) for n in dns_names]
    for address in addresses:
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            pass

    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CERT_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def start_http_redirect(https_port: int, http_port: int = 80) -> None:
    """Bounce plain HTTP to HTTPS so no password is ever sent unencrypted.

    Skipped when the setup portal is installed: that owns port 80, because it
    also has to serve the WiFi onboarding page there, and two services
    fighting over one bind is a race nobody wins.
    """
    if os.environ.get("LV_HTTP_REDIRECT", "1") == "0":
        log.info("port 80 is handled by the setup portal")
        return

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class RedirectHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - stdlib naming
            host = (self.headers.get("Host") or local_ip()).split(":")[0]
            self.send_response(301)
            self.send_header("Location", f"https://{host}:{https_port}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("0.0.0.0", http_port), RedirectHandler)
    except OSError as exc:
        log.warning("HTTP redirect listener not started on port %s: %s", http_port, exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True, name="http-redirect").start()
    log.info("redirecting http://:%s to https://:%s", http_port, https_port)


def main() -> int:
    parser = argparse.ArgumentParser(prog="little-voicemail-web")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--sounds-dir", type=Path, default=default_sounds_dir())
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--http-redirect-port", type=int, default=80)
    parser.add_argument("--no-tls", action="store_true",
                        help="serve plain HTTP (development only)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    app = create_app(args.config, args.data_dir, args.sounds_dir)
    from ..config import Config

    config = Config(args.config)
    port = args.port or int(config.get("web", "port", default=8443))
    hostname = config.get("web", "hostname", default="littlevoicemail")

    server = WSGIServer((args.host, port), app, numthreads=8, server_name="little-voicemail")

    if args.no_tls:
        log.warning("TLS disabled - do not run this way on a real device")
        app.config["SESSION_COOKIE_SECURE"] = False
        log.info("parent UI at http://%s:%s", local_ip(), port)
    else:
        cert_path, key_path = ensure_certificate(hostname)
        adapter = BuiltinSSLAdapter(str(cert_path), str(key_path))
        adapter.context.minimum_version = ssl.TLSVersion.TLSv1_2
        server.ssl_adapter = adapter
        start_http_redirect(port, args.http_redirect_port)
        log.info(
            "parent UI at https://%s.local:%s (or https://%s:%s)",
            hostname, port, local_ip(), port,
        )

    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
