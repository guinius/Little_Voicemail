"""Web UI: authentication gating and the settings forms."""

import pytest
from werkzeug.security import generate_password_hash

from src.config import Config
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
    "path", ["/", "/contacts", "/sounds", "/quiet-times", "/signal", "/system"]
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


def test_saving_a_contact_persists_it(client, paths):
    login(client)
    client.post(
        "/contacts",
        data={"name_3": "Grandma", "number_3": "+447700900123", "enabled_3": "on"},
        follow_redirects=True,
    )
    config = Config(paths[0])
    assert config.contact(3)["name"] == "Grandma"


def test_a_bad_number_is_refused(client, paths):
    login(client)
    response = client.post(
        "/contacts",
        data={"name_1": "Oops", "number_1": "07700900123", "enabled_1": "on"},
        follow_redirects=True,
    )
    assert b"not a valid international" in response.data
    assert Config(paths[0]).contact(1) is None


def test_blank_number_clears_the_slot(client, paths):
    config = Config(paths[0])
    config.set_contact(2, "Old", "+447700900999")

    login(client)
    client.post("/contacts", data={"name_2": "", "number_2": ""}, follow_redirects=True)

    assert Config(paths[0]).contact(2) is None


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
    assert response.headers["Location"].endswith("/signal")
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


def test_signal_page_offers_linking_when_unlinked(client):
    login(client)
    response = client.get("/signal")
    assert response.status_code == 200
    assert b"Link a Signal account" in response.data


def test_an_unlinked_device_says_so_on_every_page(client):
    login(client)
    assert b"No Signal account is linked" in client.get("/").data
    # ...except on the page that fixes it.
    assert b"No Signal account is linked" not in client.get("/signal").data


def test_signal_page_shows_the_account_once_linked(tmp_path):
    client = build_client(tmp_path / "config.json", tmp_path, account="+447700900123")
    login(client)
    response = client.get("/signal")
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
