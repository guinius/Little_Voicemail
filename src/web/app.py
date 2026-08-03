"""Parent web UI.

Served over HTTPS on the local network with a self-signed certificate
generated at install time, behind a single parent password. It never
listens on anything but the LAN address, and every page except the login
requires a session.

The device service and the web service are separate processes, so the UI
talks to the phone through shared state: the config file, the message
database, and a small status file the phone writes. That keeps a crash in
the web UI from taking the phone down, and vice versa.
"""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from ..audio import AudioEngine
from ..config import NUM_CONTACTS, Config
from ..messages import MessageQueue
from ..paths import (
    default_config_path,
    default_data_dir,
    default_sounds_dir,
    signal_config_dir,
)
from ..quiet_hours import QuietHours, parse_hhmm
from ..signal_link import SignalLinker
from ..updater import Updater

log = logging.getLogger(__name__)

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def create_app(
    config_path: Path | None = None,
    data_dir: Path | None = None,
    sounds_dir: Path | None = None,
    linker: SignalLinker | None = None,
) -> Flask:
    app = Flask(__name__)
    config = Config(config_path or default_config_path())
    data_dir = Path(data_dir or default_data_dir())
    sounds_dir = Path(sounds_dir or default_sounds_dir())

    queue = MessageQueue(data_dir / "messages.db")
    updater = Updater(config)
    # Injectable so tests never fork sudo, systemctl or a JVM.
    linker = linker or SignalLinker(config, signal_dir=signal_config_dir())
    quiet = QuietHours(config)
    audio = AudioEngine(config, work_dir=data_dir / "recordings", sounds_dir=sounds_dir)

    app.secret_key = _session_secret(data_dir)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    # -- auth ------------------------------------------------------------

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authenticated"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def password_is_set() -> bool:
        return bool(config.get("web", "password_hash", default=""))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password_is_set():
            return redirect(url_for("first_run"))
        if request.method == "POST":
            supplied = request.form.get("password", "")
            stored = config.get("web", "password_hash", default="")
            if check_password_hash(stored, supplied):
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                target = request.args.get("next", "")
                return redirect(target if target.startswith("/") else url_for("index"))
            flash("Incorrect password.", "error")
        return render_template("login.html")

    @app.route("/first-run", methods=["GET", "POST"])
    def first_run():
        if password_is_set():
            return redirect(url_for("login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            if len(password) < 8:
                flash("Use at least 8 characters.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            else:
                config.set(generate_password_hash(password), "web", "password_hash")
                session.clear()
                session["authenticated"] = True
                # Nothing works until a Signal account is linked, so go
                # straight there rather than to an empty status page.
                return redirect(url_for("signal_page"))
        return render_template("first_run.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -- pages -----------------------------------------------------------

    @app.route("/")
    @login_required
    def index():
        return render_template(
            "index.html",
            contacts=config.contacts(),
            pending=queue.pending_counts(),
            status=_device_status(data_dir),
            quiet_active=quiet.active_window(),
            update=updater.check() if config.get(
                "updates", "check_on_load", default=True
            ) else None,
            version=updater.local_version(),
        )

    @app.route("/contacts", methods=["GET", "POST"])
    @login_required
    def contacts():
        if request.method == "POST":
            errors = _save_contacts(config, request.form)
            for message in errors:
                flash(message, "error")
            if not errors:
                flash("Contacts saved.", "success")
            return redirect(url_for("contacts"))
        return render_template(
            "contacts.html",
            contacts=config.contacts(),
            num_contacts=NUM_CONTACTS,
            pending=queue.pending_counts(),
        )

    @app.route("/sounds", methods=["GET", "POST"])
    @login_required
    def sounds():
        available = audio.available_ringtones()
        if request.method == "POST":
            chosen = request.form.get("ringtone", "")
            if chosen not in available:
                flash("Unknown ringtone.", "error")
            else:
                config.set(chosen, "audio", "ringtone")
                volume = _clamp_float(request.form.get("volume"), 0.0, 1.0, 0.8)
                config.set(volume, "audio", "ringtone_volume")
                flash("Ringtone saved.", "success")
            return redirect(url_for("sounds"))
        return render_template(
            "sounds.html",
            ringtones=available,
            current=config.get("audio", "ringtone", default=""),
            volume=config.get("audio", "ringtone_volume", default=0.8),
        )

    @app.route("/quiet-times", methods=["GET", "POST"])
    @login_required
    def quiet_times():
        if request.method == "POST":
            errors = _save_quiet_times(config, request.form)
            for message in errors:
                flash(message, "error")
            if not errors:
                flash("Quiet times saved.", "success")
            return redirect(url_for("quiet_times"))
        return render_template(
            "quiet_times.html",
            windows=config.get("quiet_times", default=[]),
            day_names=DAY_NAMES,
            active=quiet.active_window(),
        )

    @app.route("/signal", methods=["GET"])
    @login_required
    def signal_page():
        return render_template(
            "signal.html",
            account=linker.account,
            link=linker.snapshot(),
            services=linker.service_states(),
            signal_cli=linker.available,
        )

    @app.route("/system", methods=["GET"])
    @login_required
    def system():
        return render_template(
            "system.html",
            update=updater.check(force=request.args.get("recheck") == "1"),
            progress=updater.progress,
            version=updater.local_version(),
            account=config.get("signal", "account", default=""),
            status=_device_status(data_dir),
            recent=queue.recent(limit=25),
        )

    # -- actions ---------------------------------------------------------

    @app.route("/api/queue/reset", methods=["POST"])
    @login_required
    def reset_queue():
        slot = request.json.get("slot") if request.is_json else request.form.get("slot")
        if slot in (None, "", "all"):
            cleared = queue.clear_all()
        else:
            cleared = queue.clear_slot(int(slot))
        log.info("parent cleared %d queued message(s)", cleared)
        return jsonify({"cleared": cleared, "pending": queue.pending_counts()})

    @app.route("/api/status")
    @login_required
    def api_status():
        return jsonify(
            {
                "pending": queue.pending_counts(),
                "total_pending": queue.total_pending(),
                "quiet": quiet.is_quiet(),
                "device": _device_status(data_dir),
            }
        )

    @app.route("/api/update/check")
    @login_required
    def api_update_check():
        status = updater.check(force=True)
        return jsonify(status.__dict__)

    @app.route("/api/update/start", methods=["POST"])
    @login_required
    def api_update_start():
        if not updater.check().update_available:
            return jsonify({"error": "no update available"}), 400
        if not updater.start_update(reboot=True):
            return jsonify({"error": "an update is already running"}), 409
        return jsonify({"started": True})

    @app.route("/api/update/progress")
    @login_required
    def api_update_progress():
        progress = updater.progress
        return jsonify(
            {
                "running": progress.running,
                "finished": progress.finished,
                "ok": progress.ok,
                "message": progress.message,
                "log": progress.log_lines[-12:],
            }
        )

    @app.route("/api/contacts/import", methods=["POST"])
    @login_required
    def api_contacts_import():
        """Pull display names for the configured numbers from Signal."""
        from ..signal_client import SignalClient
        import asyncio

        async def fetch():
            client = SignalClient(
                account=config.get("signal", "account", default=""),
                host=config.get("signal", "jsonrpc_host", default="127.0.0.1"),
                port=int(config.get("signal", "jsonrpc_port", default=7583)),
            )
            client.start()
            try:
                if not await client.wait_connected(timeout=5):
                    raise ConnectionError("signal-cli is not running")
                return await client.list_contacts()
            finally:
                await client.stop()

        try:
            known = asyncio.run(fetch())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502

        by_number = {_digits(c["number"]): c["name"] for c in known if c["name"]}
        updated = []
        for entry in config.contacts():
            name = by_number.get(_digits(entry["number"]))
            if entry["number"] and name and name != entry["name"]:
                config.set_contact(entry["slot"], name, entry["number"], entry["enabled"])
                updated.append({"slot": entry["slot"], "name": name})
        return jsonify({"updated": updated, "known": len(by_number)})

    # -- signal linking --------------------------------------------------
    #
    # No CSRF token: nothing in this app has one, and SESSION_COOKIE_SAMESITE
    # ="Lax" already stops a cross-site POST from carrying the session.

    @app.route("/api/signal/link/start", methods=["POST"])
    @login_required
    def api_signal_link_start():
        force = bool(_json_field(request, "force"))
        started, reason = linker.start(force=force)
        if not started:
            return jsonify({"error": reason}), 409
        return jsonify({"started": True, "link": linker.snapshot()})

    @app.route("/api/signal/link/status")
    @login_required
    def api_signal_link_status():
        return jsonify(
            {
                "link": linker.snapshot(),
                "account": linker.account,
                "services": linker.service_states(),
            }
        )

    @app.route("/api/signal/link/cancel", methods=["POST"])
    @login_required
    def api_signal_link_cancel():
        return jsonify({"cancelled": linker.cancel()})

    @app.route("/api/signal/link/qr.svg")
    @login_required
    def api_signal_link_qr():
        """The URI as a QR code, for the iOS path that needs a second screen."""
        uri = linker.snapshot()["uri"]
        if not uri:
            return jsonify({"error": "no link in progress"}), 404
        try:
            import segno
        except ImportError:
            return jsonify({"error": "QR rendering is unavailable"}), 501
        buffer = io.BytesIO()
        segno.make(uri, error="m").save(
            buffer, kind="svg", scale=8, border=2, dark="#111", light="#fff"
        )
        response = Response(buffer.getvalue(), mimetype="image/svg+xml")
        # A provisioning URI is a secret and a short-lived one.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/signal/unlink", methods=["POST"])
    @login_required
    def api_signal_unlink():
        ok, detail = linker.unlink()
        if not ok:
            return jsonify({"error": detail}), 500
        return jsonify({"ok": True, "parked": detail})

    @app.context_processor
    def inject_globals():
        return {
            "app_version": updater.local_version(),
            "quiet_now": quiet.is_quiet(),
            "signal_linked": bool(config.get("signal", "account", default="")),
        }

    return app


# -- helpers -------------------------------------------------------------


def _save_contacts(config: Config, form) -> list[str]:
    errors: list[str] = []
    for slot in range(1, NUM_CONTACTS + 1):
        name = (form.get(f"name_{slot}") or "").strip()
        number = (form.get(f"number_{slot}") or "").strip().replace(" ", "")
        enabled = form.get(f"enabled_{slot}") == "on"

        if not number:
            config.clear_contact(slot)
            continue
        if not E164.match(number):
            errors.append(
                f"Button {slot}: '{number}' is not a valid international "
                f"number (for example +447700900123)."
            )
            continue
        if not name:
            name = number
        config.set_contact(slot, name, number, enabled)
    return errors


def _save_quiet_times(config: Config, form) -> list[str]:
    errors: list[str] = []
    windows = config.get("quiet_times", default=[]) or []
    updated = []
    for window in windows:
        wid = window.get("id")
        start = (form.get(f"start_{wid}") or window.get("start", "00:00")).strip()
        end = (form.get(f"end_{wid}") or window.get("end", "00:00")).strip()
        try:
            parse_hhmm(start)
            parse_hhmm(end)
        except (ValueError, TypeError):
            errors.append(f"{window.get('label', wid)}: times must be HH:MM.")
            updated.append(window)
            continue
        if start == end:
            errors.append(
                f"{window.get('label', wid)}: start and end cannot be the same."
            )
            updated.append(window)
            continue
        days = [int(d) for d in form.getlist(f"days_{wid}") if d.isdigit()]
        updated.append(
            {
                **window,
                "enabled": form.get(f"enabled_{wid}") == "on",
                "start": start,
                "end": end,
                "days": days or list(range(7)),
            }
        )
    if not errors:
        config.set(updated, "quiet_times")
    return errors


def _device_status(data_dir: Path) -> dict:
    """Read the status file the phone service writes each few seconds."""
    path = Path(data_dir) / "status.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            status = json.load(fh)
        status["stale"] = False
        return status
    except (OSError, json.JSONDecodeError):
        return {"stale": True, "state": "unknown", "signal_connected": False}


def _session_secret(data_dir: Path) -> bytes:
    """Persist the Flask secret so a restart doesn't log the parent out."""
    path = Path(data_dir) / "session.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes()
    secret = secrets.token_bytes(32)
    path.write_bytes(secret)
    path.chmod(0o600)
    return secret


def _clamp_float(value, low: float, high: float, fallback: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


def _digits(number: str) -> str:
    return "".join(ch for ch in (number or "") if ch.isdigit())


def _json_field(req, name: str):
    if req.is_json:
        return (req.get_json(silent=True) or {}).get(name)
    return req.form.get(name)
