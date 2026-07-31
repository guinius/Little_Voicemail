"""Web UI: authentication gating and the settings forms."""

import pytest
from werkzeug.security import generate_password_hash

from src.config import Config
from src.web.app import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def paths(tmp_path):
    config_path = tmp_path / "config.json"
    config = Config(config_path)
    config.set(generate_password_hash(PASSWORD), "web", "password_hash")
    return config_path, tmp_path / "data", tmp_path / "sounds"


@pytest.fixture
def client(paths):
    config_path, data_dir, sounds_dir = paths
    sounds_dir.mkdir(parents=True, exist_ok=True)
    (sounds_dir / "chime.wav").write_bytes(b"RIFF")
    app = create_app(config_path, data_dir, sounds_dir)
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.test_client()


def login(client):
    return client.post("/login", data={"password": PASSWORD}, follow_redirects=False)


@pytest.mark.parametrize(
    "path", ["/", "/contacts", "/sounds", "/quiet-times", "/system"]
)
def test_pages_require_a_password(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("path", ["/api/status", "/api/update/check"])
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


def test_first_run_sets_a_password(tmp_path):
    config_path = tmp_path / "config.json"
    Config(config_path)  # no password yet
    app = create_app(config_path, tmp_path / "data", tmp_path / "sounds")
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = app.test_client()

    assert "/first-run" in client.get("/login").headers["Location"]

    client.post(
        "/first-run",
        data={"password": "a-good-password", "confirm": "a-good-password"},
    )
    assert Config(config_path).get("web", "password_hash")
    assert client.get("/").status_code == 200


def test_first_run_rejects_a_short_password(tmp_path):
    config_path = tmp_path / "config.json"
    Config(config_path)
    app = create_app(config_path, tmp_path / "data", tmp_path / "sounds")
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    client = app.test_client()

    response = client.post(
        "/first-run", data={"password": "short", "confirm": "short"}
    )
    assert b"at least 8 characters" in response.data
    assert not Config(config_path).get("web", "password_hash")
