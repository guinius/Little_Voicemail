"""HTTPS entry point for the parent web UI.

Requirement 8 is "connect securely from a device on the same local network".
There is no public hostname to get a real certificate for, so the box mints
its own self-signed certificate at first boot, valid for its hostname, its
.local mDNS name and its LAN address. Browsers will warn once; a parent
accepts it and the connection is encrypted from then on.

Plain HTTP is not served at all - only a redirect listener that bounces to
the HTTPS port, so a mistyped http:// never sends the parent password in
the clear.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import logging
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


def ensure_certificate(hostname: str) -> tuple[Path, Path]:
    """Return (cert, key), generating a self-signed pair if needed."""
    directory = certs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    log.info("generating a self-signed certificate for %s", hostname)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Little Voicemail"),
        ]
    )
    address = local_ip()
    alt_names: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName(f"{hostname}.local"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    try:
        alt_names.append(x509.IPAddress(ipaddress.ip_address(address)))
    except ValueError:
        pass

    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CERT_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
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
    """Bounce plain HTTP to HTTPS so no password is ever sent unencrypted."""
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
