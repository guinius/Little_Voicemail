"""Web UI: authentication gating and the settings forms."""

import io
import time

import pytest
from werkzeug.security import generate_password_hash

from src.config import Config
from src.messages import MessageQueue
from src.signal_link import SignalLinker
from src.web.app import create_app

PASSWORD = "correct horse battery"


def stub_linker(config, tmp_path):
    """A linker that never forks sudo, systemctl or a JVM."""
    if not isinstance(config, Config):
        config = Config(config)
    return SignalLinker(
        config,
        signal_dir=tmp_path / "signal-cli",
        binary=str(tmp_path / "nonexistent-signal-cli"),
        env_path=tmp_path / "signal.env",
        runner=lambda command: (1, ""),
    )


def build_client(config_path, tmp_path, account=""):
    """An app whose config, linker and test client all share one Config.

    Config instances cache on load, so a second one built from the same path
    would not see writes made through the first.
    """
    config = Config(config_path)
    config.set(generate_password_hash(PASSWORD), "web", "password_hash")
    if account:
        config.set(account, "signal", "account")
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir(parents=True, exist_ok=True)
    (sounds_dir / "chime.wav").write_bytes(b"RIFF")
    app = create_app(
        config_path,
        tmp_path / "data",
        sounds_dir,
        linker=stub_linker(config, tmp_path),
    )
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.test_client()


@pytest.fixture
def paths(tmp_path):
    config_path = tmp_path / "config.json"
    config = Config(config_path)
    config.set(generate_password_hash(PASSWORD), "web", "password_hash")
    return config_path, tmp_path / "data", tmp_path / "sounds"


@pytest.fixture
def linker(paths, tmp_path):
    return stub_linker(paths[0], tmp_path)


@pytest.fixture
def client(paths, linker):
    config_path, data_dir, sounds_dir = paths
    sounds_dir.mkdir(parents=True, exist_ok=True)
    (sounds_dir / "chime.wav").write_bytes(b"RIFF")
    app = create_app(config_path, data_dir, sounds_dir, linker=linker)
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.test_client()


def login(client):
    return client.post("/login", data={"password": PASSWORD}, follow_redirects=False)


@pytest.mark.parametrize(
    "path",
    ["/", "/contacts", "/sounds", "/quiet-times", "/signal", "/system", "/button-test"],
)
def test_pages_require_a_password(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/update/check",
        "/api/signal/link/status",
        "/api/signal/link/qr.svg",
    ],
)
def test_api_returns_401_rather_than_a_redirect(client, path):
    assert client.get(path).status_code == 401


def test_wrong_password_is_rejected(client):
    response = client.post("/login", data={"password": "nope"})
    assert response.status_code == 200  # re-renders the form
    assert client.get("/").status_code == 302


def test_correct_password_grants_access(client):
    assert login(client).status_code == 302
    assert client.get("/").status_code == 200


def test_logout_ends_the_session(client):
    login(client)
    client.get("/logout")
    assert client.get("/").status_code == 302


def test_system_page_renders_with_no_audio_tools_on_the_box(client):
    """The dev/test environment has none of arecord/ffmpeg/ffplay/i2cdetect
    on PATH - the diagnostics have to degrade to "missing", not crash the
    page a parent is staring at trying to figure out what's wrong."""
    login(client)
    response = client.get("/system")
    assert response.status_code == 200
    assert b"Audio &amp; tools" in response.data or b"Audio & tools" in response.data


def test_button_test_page_redirects_to_system(client):
    """Button test moved under System -> Advanced to cut down on tabs."""
    login(client)
    response = client.get("/button-test")
    assert response.status_code == 302
    assert "/system" in response.headers["Location"]
    assert "advanced=1" in response.headers["Location"]


def test_system_page_has_the_button_test_content(client):
    login(client)
    response = client.get("/system")
    assert response.status_code == 200
    assert b"Button test" in response.data


def test_system_page_has_the_update_channel_content(client):
    login(client)
    response = client.get("/system")
    assert response.status_code == 200
    assert b"Update channel" in response.data


def test_saving_the_update_channel(client, paths):
    login(client)
    config_path, _, _ = paths
    response = client.post(
        "/system", data={"update_branch": "dev"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert Config(config_path).get("updates", "branch") == "dev"
    assert b"dev" in response.data


def test_saving_an_unknown_update_channel_is_rejected(client, paths):
    login(client)
    config_path, _, _ = paths
    response = client.post(
        "/system", data={"update_branch": "not-a-real-branch"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"Unknown update channel" in response.data
    assert Config(config_path).get("updates", "branch") == "master"


def test_starting_button_test_creates_the_flag_file(client, paths):
    login(client)
    _, data_dir, _ = paths
    flag = data_dir / "test_mode.flag"
    assert not flag.exists()

    response = client.post("/api/button-test/start")

    assert response.status_code == 200
    assert response.get_json()["active"] is True
    assert flag.exists()


def test_stopping_button_test_removes_the_flag_file(client, paths):
    login(client)
    _, data_dir, _ = paths
    flag = data_dir / "test_mode.flag"
    client.post("/api/button-test/start")
    assert flag.exists()

    response = client.post("/api/button-test/stop")

    assert response.status_code == 200
    assert response.get_json()["active"] is False
    assert not flag.exists()


def test_system_page_reflects_the_flag_file_on_load(client, paths):
    login(client)
    _, data_dir, _ = paths
    (data_dir / "test_mode.flag").parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "test_mode.flag").write_text("", encoding="utf-8")

    response = client.get("/system")

    assert response.status_code == 200
    assert b"Stop test mode" in response.data


def test_certificate_page_needs_no_login(client):
    """A parent hitting the browser warning hasn't signed in yet."""
    response = client.get("/certificate")
    assert response.status_code == 200
    assert b"Install the certificate" in response.data


def test_ca_download_is_404_before_the_server_has_ever_started(client):
    """The web test app never calls ensure_certificate()/ensure_ca() (those
    live in server.py, exercised separately in test_certificate.py) - so on
    a fresh temp dir there is no CA file yet, and this must say so rather
    than 500."""
    response = client.get("/ca.crt")
    assert response.status_code == 404


def test_ca_download_serves_the_certificate_once_present(client, paths):
    _, data_dir, _ = paths
    certs = data_dir / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "ca.crt").write_bytes(b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

    response = client.get("/ca.crt")

    assert response.status_code == 200
    assert response.mimetype == "application/x-x509-ca-cert"
    assert b"BEGIN CERTIFICATE" in response.data


def test_factory_reset_requires_login(client):
    assert client.post("/api/factory-reset", json={"confirm": "RESET"}).status_code == 401


def test_factory_reset_requires_typing_reset(client):
    login(client)
    response = client.post("/api/factory-reset", json={"confirm": "nope"})
    assert response.status_code == 400
    assert "RESET" in response.get_json()["error"]


def test_factory_reset_starts_when_confirmed(client, paths, monkeypatch):
    login(client)
    import src.factory_reset as factory_reset

    calls = []
    monkeypatch.setattr(factory_reset, "wipe", lambda *a, **k: calls.append("wipe"))
    monkeypatch.setattr(factory_reset, "reboot", lambda: calls.append("reboot") or (True, ""))

    response = client.post("/api/factory-reset", json={"confirm": "reset"})

    assert response.status_code == 200
    assert response.get_json()["started"] is True
    # Runs in a background thread with a short delay - give it a moment.
    for _ in range(50):
        if calls == ["wipe", "reboot"]:
            break
        time.sleep(0.05)
    assert calls == ["wipe", "reboot"]


def test_button_test_apis_require_login(client):
    assert client.post("/api/button-test/start").status_code == 401
    assert client.post("/api/button-test/stop").status_code == 401


def test_contacts_page_redirects_to_status(client):
    """Contacts moved onto the Status page's button tiles."""
    login(client)
    response = client.get("/contacts")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_saving_a_contact_persists_it(client, paths):
    login(client)
    response = client.post(
        "/api/contacts/3",
        json={"name": "Grandma", "number": "+447700900123", "enabled": True},
    )
    assert response.status_code == 200
    assert response.get_json()["contact"]["name"] == "Grandma"
    config = Config(paths[0])
    assert config.contact(3)["name"] == "Grandma"


def test_a_bad_number_is_refused(client, paths):
    login(client)
    response = client.post(
        "/api/contacts/1",
        json={"name": "Oops", "number": "07700900123", "enabled": True},
    )
    assert response.status_code == 400
    assert "not a valid international" in response.get_json()["error"]
    assert Config(paths[0]).contact(1) is None


def test_blank_number_clears_the_slot(client, paths):
    config = Config(paths[0])
    config.set_contact(2, "Old", "+447700900999")

    login(client)
    client.post("/api/contacts/2", json={"name": "", "number": ""})

    assert Config(paths[0]).contact(2) is None


def test_save_contact_requires_login(client):
    response = client.post("/api/contacts/1", json={"name": "Oops", "number": ""})
    assert response.status_code == 401


def test_save_contact_rejects_an_out_of_range_slot(client):
    login(client)
    response = client.post("/api/contacts/9", json={"name": "X", "number": "+447700900123"})
    assert response.status_code == 404


def test_quiet_times_save(client, paths):
    login(client)
    client.post(
        "/quiet-times",
        data={
            "enabled_bedtime": "on",
            "start_bedtime": "19:30",
            "end_bedtime": "06:45",
            "days_bedtime": ["0", "1", "2"],
        },
        follow_redirects=True,
    )
    windows = {w["id"]: w for w in Config(paths[0]).get("quiet_times")}
    assert windows["bedtime"]["enabled"] is True
    assert windows["bedtime"]["start"] == "19:30"
    assert windows["bedtime"]["days"] == [0, 1, 2]
    # Untouched windows keep their settings.
    assert windows["school"]["enabled"] is False


def test_identical_start_and_end_is_refused(client, paths):
    login(client)
    response = client.post(
        "/quiet-times",
        data={"enabled_nap": "on", "start_nap": "13:00", "end_nap": "13:00"},
        follow_redirects=True,
    )
    assert b"cannot be the same" in response.data


def test_ringtone_must_be_one_that_exists(client, paths):
    login(client)
    response = client.post(
        "/sounds", data={"ringtone": "../../etc/passwd"}, follow_redirects=True
    )
    assert b"Unknown ringtone" in response.data
    assert Config(paths[0]).get("audio", "ringtone") == "chime.wav"


def test_ringtone_selection_saves(client, paths):
    login(client)
    client.post(
        "/sounds", data={"ringtone": "chime.wav", "volume": "0.5"}, follow_redirects=True
    )
    config = Config(paths[0])
    assert config.get("audio", "ringtone") == "chime.wav"
    assert config.get("audio", "ringtone_volume") == 0.5


def test_upload_requires_login(client):
    response = client.post("/api/sounds/upload", data={})
    assert response.status_code == 401


def test_upload_rejects_no_file(client):
    login(client)
    response = client.post("/api/sounds/upload", data={})
    assert response.status_code == 400
    assert "Choose a sound file" in response.get_json()["error"]


def test_upload_rejects_a_disallowed_extension(client):
    login(client)
    data = {"file": (io.BytesIO(b"not really audio"), "creepy.exe")}
    response = client.post(
        "/api/sounds/upload", data=data, content_type="multipart/form-data"
    )
    assert response.status_code == 400
    assert ".wav, .mp3 or .ogg" in response.get_json()["error"]


def test_uploading_a_wav_adds_it_to_the_dropdown(client, paths):
    """No ffprobe on the test box, so the file just needs the right name and
    extension - the same "skip, don't fail, when we can't check" rule the
    audio diagnostics on the System page already follow."""
    login(client)
    data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "lullaby.wav")}
    response = client.post(
        "/api/sounds/upload", data=data, content_type="multipart/form-data"
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["name"] == "lullaby.wav"
    assert "lullaby.wav" in body["ringtones"]
    _, _, sounds_dir = paths
    assert (sounds_dir / "lullaby.wav").exists()


def test_uploading_an_mp3_works_just_like_a_wav(client, paths):
    """The point of item 1: an mp3 needs no special handling - it is
    accepted, saved, and offered in the dropdown the same as a .wav."""
    login(client)
    data = {"file": (io.BytesIO(b"ID3\x03\x00\x00\x00fake mp3 bytes"), "chime.mp3")}
    response = client.post(
        "/api/sounds/upload", data=data, content_type="multipart/form-data"
    )
    assert response.status_code == 200
    assert response.get_json()["name"] == "chime.mp3"
    _, _, sounds_dir = paths
    assert (sounds_dir / "chime.mp3").exists()


def test_uploading_a_duplicate_name_does_not_overwrite(client, paths):
    login(client)
    _, _, sounds_dir = paths
    data1 = {"file": (io.BytesIO(b"first"), "hello.wav")}
    client.post("/api/sounds/upload", data=data1, content_type="multipart/form-data")
    data2 = {"file": (io.BytesIO(b"second"), "hello.wav")}
    response = client.post(
        "/api/sounds/upload", data=data2, content_type="multipart/form-data"
    )
    assert response.status_code == 200
    assert response.get_json()["name"] == "hello-1.wav"
    assert (sounds_dir / "hello.wav").read_bytes() == b"first"
    assert (sounds_dir / "hello-1.wav").read_bytes() == b"second"


def test_delete_requires_login(client):
    response = client.post("/api/sounds/delete", json={"name": "chime.wav"})
    assert response.status_code == 401


def test_delete_rejects_an_unknown_sound(client):
    login(client)
    response = client.post("/api/sounds/delete", json={"name": "../../etc/passwd"})
    assert response.status_code == 400
    assert "Unknown sound" in response.get_json()["error"]


def test_delete_removes_the_file_and_updates_the_list(client, paths):
    login(client)
    _, _, sounds_dir = paths
    data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "lullaby.wav")}
    client.post("/api/sounds/upload", data=data, content_type="multipart/form-data")
    assert (sounds_dir / "lullaby.wav").exists()

    response = client.post("/api/sounds/delete", json={"name": "lullaby.wav"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["deleted"] == "lullaby.wav"
    assert "lullaby.wav" not in body["ringtones"]
    assert not (sounds_dir / "lullaby.wav").exists()


def test_deleting_the_selected_ringtone_falls_back_to_another_one(client, paths):
    login(client)
    config_path, _, sounds_dir = paths
    data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "lullaby.wav")}
    client.post("/api/sounds/upload", data=data, content_type="multipart/form-data")
    client.post("/sounds", data={"ringtone": "lullaby.wav", "volume": "0.5"})
    assert Config(config_path).get("audio", "ringtone") == "lullaby.wav"

    response = client.post("/api/sounds/delete", json={"name": "lullaby.wav"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["current"] == "chime.wav"
    assert Config(config_path).get("audio", "ringtone") == "chime.wav"


def test_preview_requires_login(client):
    response = client.post("/api/sounds/preview", json={"name": "chime.wav"})
    assert response.status_code == 401


def test_preview_rejects_an_unknown_ringtone(client):
    login(client)
    response = client.post("/api/sounds/preview", json={"name": "nope.wav"})
    assert response.status_code == 400
    assert "Unknown ringtone" in response.get_json()["error"]


def test_preview_fails_gracefully_with_no_ffplay(client):
    """The dev/test box has no ffplay on PATH - same as a fresh Pi before
    install.sh runs. The route has to say so, not 500 with a traceback."""
    login(client)
    response = client.post("/api/sounds/preview", json={"name": "chime.wav"})
    assert response.status_code == 500
    assert "ffplay" in response.get_json()["error"]


def test_first_run_sets_a_password_and_leads_to_linking(tmp_path):
    config_path = tmp_path / "config.json"
    Config(config_path)  # no password yet
    app = create_app(
        config_path,
        tmp_path / "data",
        tmp_path / "sounds",
        linker=stub_linker(config_path, tmp_path),
    )
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = app.test_client()

    assert "/first-run" in client.get("/login").headers["Location"]

    response = client.post(
        "/first-run",
        data={"password": "a-good-password", "confirm": "a-good-password"},
    )
    assert Config(config_path).get("web", "password_hash")
    # Nothing works without a Signal account, so that is the next step.
    assert response.headers["Location"].endswith("/system#signal-section")
    assert client.get("/").status_code == 200


def test_first_run_rejects_a_short_password(tmp_path):
    config_path = tmp_path / "config.json"
    Config(config_path)
    app = create_app(
        config_path,
        tmp_path / "data",
        tmp_path / "sounds",
        linker=stub_linker(config_path, tmp_path),
    )
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = app.test_client()

    response = client.post(
        "/first-run", data={"password": "short", "confirm": "short"}
    )
    assert b"at least 8 characters" in response.data
    assert not Config(config_path).get("web", "password_hash")


# -- the Signal page -------------------------------------------------------


def test_signal_page_redirects_to_system(client):
    """The Signal tab was folded into System to cut down on tabs."""
    login(client)
    response = client.get("/signal")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/system#signal-section")


def test_system_page_offers_linking_when_unlinked(client):
    login(client)
    response = client.get("/system")
    assert response.status_code == 200
    assert b"Link a Signal account" in response.data


def test_an_unlinked_device_says_so_on_every_page(client):
    login(client)
    assert b"No Signal account is linked" in client.get("/").data
    # ...except on the page that fixes it.
    assert b"No Signal account is linked" not in client.get("/system").data


def test_system_page_shows_the_account_once_linked(tmp_path):
    client = build_client(tmp_path / "config.json", tmp_path, account="+447700900123")
    login(client)
    response = client.get("/system")
    assert b"+447700900123" in response.data
    assert b"Unlink this device" in response.data
    assert b"No Signal account is linked" not in client.get("/").data


def test_linking_is_refused_when_signal_cli_is_missing(client):
    login(client)
    response = client.post("/api/signal/link/start", json={})
    assert response.status_code == 409
    assert "signal-cli is not installed" in response.get_json()["error"]


def test_linking_is_refused_when_already_linked(tmp_path):
    client = build_client(tmp_path / "config.json", tmp_path, account="+447700900123")
    login(client)
    response = client.post("/api/signal/link/start", json={})
    assert response.status_code == 409
    assert "already linked" in response.get_json()["error"]


def test_qr_is_404_with_no_link_in_progress(client):
    login(client)
    assert client.get("/api/signal/link/qr.svg").status_code == 404


def test_qr_renders_an_svg_for_a_live_link(client, linker):
    login(client)
    linker._set(phase="waiting", uri="sgnl://linkdevice?uuid=abc&pub_key=def")
    response = client.get("/api/signal/link/qr.svg")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert response.headers["Cache-Control"] == "no-store"
    assert b"<svg" in response.data


def test_status_reports_the_phase(client, linker):
    login(client)
    linker._set(phase="waiting", uri="sgnl://linkdevice?uuid=abc")
    body = client.get("/api/signal/link/status").get_json()
    assert body["link"]["phase"] == "waiting"
    assert body["link"]["uri"].startswith("sgnl://")


# -- recent messages / requeue --------------------------------------------


def test_a_played_message_offers_play_again_on_the_status_page(client, paths):
    _, data_dir, _ = paths
    queue = MessageQueue(data_dir / "messages.db")
    message_id = queue.add(slot=1, sender="+441", signal_ts=1, attachment="/tmp/a.ogg")
    queue.mark_played(message_id)
    queue.close()

    login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert b"Played on Little Voicemail" in response.data
    assert b'class="ghost requeue-btn"' in response.data


def test_the_requeue_script_targets_the_actual_status_element(client):
    """Regression: the status span was renamed from .badge to .msg-status
    (a multi-word phrase looks broken in the pill-shaped .badge), but the
    "Play again" handler still looked up '.badge' - a bug that only shows
    up by clicking the button in a real browser, since nothing here runs
    the page's JS. row.querySelector('.badge') then returns null and the
    click handler throws before it can update the row or the button tile,
    even though the server-side requeue already succeeded.
    """
    login(client)
    body = client.get("/").data.decode()
    handler = body[body.index("messages-body"):]
    assert "querySelector('.msg-status')" in handler
    assert "querySelector('.badge')" not in handler


def test_a_still_waiting_message_has_no_play_again_button(client, paths):
    _, data_dir, _ = paths
    queue = MessageQueue(data_dir / "messages.db")
    queue.add(slot=1, sender="+441", signal_ts=1, attachment="/tmp/a.ogg")
    queue.close()

    login(client)
    response = client.get("/")
    assert b"Waiting on the box" in response.data
    assert b'class="ghost requeue-btn"' not in response.data


def test_the_status_page_shows_only_the_last_six_messages(client, paths):
    _, data_dir, _ = paths
    queue = MessageQueue(data_dir / "messages.db")
    for i in range(9):
        queue.add(slot=1, sender="+441", signal_ts=i, attachment=f"/tmp/{i}.ogg")
    queue.close()

    login(client)
    response = client.get("/")
    assert response.data.count(b'<tr data-id="') == 6


def test_requeue_puts_a_played_message_back_in_the_queue(client, paths):
    _, data_dir, _ = paths
    queue = MessageQueue(data_dir / "messages.db")
    message_id = queue.add(slot=3, sender="+441", signal_ts=1, attachment="/tmp/a.ogg")
    queue.mark_played(message_id)
    queue.close()

    login(client)
    response = client.post("/api/queue/requeue", json={"id": message_id})
    assert response.status_code == 200
    body = response.get_json()
    assert body["requeued"] is True
    assert body["pending"] == {"3": 1}

    queue = MessageQueue(data_dir / "messages.db")
    assert [m.id for m in queue.pending_for_slot(3)] == [message_id]
    queue.close()


def test_requeue_rejects_a_message_that_is_still_waiting(client, paths):
    _, data_dir, _ = paths
    queue = MessageQueue(data_dir / "messages.db")
    message_id = queue.add(slot=1, sender="+441", signal_ts=1, attachment="/tmp/a.ogg")
    queue.close()

    login(client)
    response = client.post("/api/queue/requeue", json={"id": message_id})
    assert response.status_code == 404


def test_requeue_rejects_an_unknown_message_id(client):
    login(client)
    response = client.post("/api/queue/requeue", json={"id": 999})
    assert response.status_code == 404


def test_requeue_requires_authentication(client):
    response = client.post("/api/queue/requeue", json={"id": 1})
    assert response.status_code == 401
