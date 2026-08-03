"""Linking a Signal account.

These drive a fake signal-cli rather than mocking Popen: the part most
likely to be wrong is the pipe handling - signal-cli prints the provisioning
URI and then blocks, possibly without a trailing newline - and a mock would
not exercise it at all.
"""

import json
import os
import stat
import sys
import time

import pytest

from src.config import Config
from src.signal_link import SignalLinker

URI = "sgnl://linkdevice?uuid=RaBBiT&pub_key=Y2Fycm90cw"
NUMBER = "+447700900123"

FAKE = '''#!/usr/bin/env python3
import json, os, sys, time

args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")

if "listAccounts" in args:
    if mode in ("no-account", "uri-only"):
        print("[]")
    elif mode == "bad-number":
        print(json.dumps([{{"number": "; rm -rf /"}}]))
    else:
        print(json.dumps([{{"number": "{number}"}}]))
    sys.exit(0)

if "link" in args:
    if mode == "explode":
        sys.stderr.write("Failed to link device: connection refused\\n")
        sys.exit(3)
    if mode == "no-uri":
        time.sleep(30)
        sys.exit(0)
    if mode == "no-newline":
        # The URI with nothing after it: a line-oriented reader would block.
        sys.stdout.write("{uri}")
        sys.stdout.flush()
        time.sleep(30)
        sys.exit(0)
    print("{uri}", flush=True)
    if mode == "hang":
        time.sleep(30)
    else:
        time.sleep(0.2)
    print("Associated with: {number}", flush=True)
    sys.exit(0)

sys.exit(0)
'''


@pytest.fixture
def fake_cli(tmp_path):
    path = tmp_path / "fake-signal-cli"
    path.write_text(FAKE.format(uri=URI, number=NUMBER), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    # The fake is a python script; make sure it runs with this interpreter.
    path.write_text(
        FAKE.format(uri=URI, number=NUMBER).replace(
            "#!/usr/bin/env python3", f"#!{sys.executable}"
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def linker(tmp_path, fake_cli):
    calls = []

    def runner(command):
        calls.append(list(command))
        return 0, ""

    made = SignalLinker(
        Config(tmp_path / "config.json"),
        signal_dir=tmp_path / "signal-cli",
        binary=str(fake_cli),
        env_path=tmp_path / "signal.env",
        runner=runner,
    )
    made.calls = calls
    return made


def wait_for(linker, *phases, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        phase = linker.snapshot()["phase"]
        if phase in phases:
            return phase
        time.sleep(0.05)
    raise AssertionError(f"stuck in {linker.snapshot()['phase']}, wanted {phases}")


def with_mode(monkeypatch, mode):
    monkeypatch.setenv("FAKE_MODE", mode)


# -- the happy path --------------------------------------------------------


def test_linking_captures_the_uri_then_settles(linker, monkeypatch):
    with_mode(monkeypatch, "ok")
    started, reason = linker.start()
    assert started, reason

    wait_for(linker, "waiting")
    assert linker.snapshot()["uri"] == URI

    wait_for(linker, "linked")
    assert linker.snapshot()["account"] == NUMBER
    assert linker._config.get("signal", "account") == NUMBER


def test_the_number_lands_in_the_environment_file_too(linker, monkeypatch):
    with_mode(monkeypatch, "ok")
    linker.start()
    wait_for(linker, "linked")
    assert f"SIGNAL_ACCOUNT={NUMBER}" in linker.env_path.read_text()


def test_the_services_are_enabled_with_the_exact_sudoers_command(
    linker, monkeypatch
):
    with_mode(monkeypatch, "ok")
    linker.start()
    wait_for(linker, "linked")
    assert [
        "sudo", "-n", "/usr/bin/systemctl", "enable", "--now",
        "signal-cli.service", "little-voicemail.service",
    ] in linker.calls


def test_the_daemon_is_stopped_before_linking(linker, monkeypatch):
    """Two signal-cli processes cannot hold the account directory at once."""
    with_mode(monkeypatch, "ok")
    linker.start()
    wait_for(linker, "linked")
    assert [
        "sudo", "-n", "/usr/bin/systemctl", "stop", "signal-cli.service"
    ] in linker.calls


# -- pipe handling ---------------------------------------------------------


def test_a_uri_without_a_trailing_newline_is_still_seen(linker, monkeypatch):
    with_mode(monkeypatch, "no-newline")
    linker.start()
    wait_for(linker, "waiting", timeout=10)
    assert linker.snapshot()["uri"] == URI
    linker.cancel()


def test_the_uri_never_reaches_the_log(linker, monkeypatch):
    with_mode(monkeypatch, "ok")
    linker.start()
    wait_for(linker, "linked")
    joined = "\n".join(linker.snapshot()["log"])
    assert "RaBBiT" not in joined
    assert "Y2Fycm90cw" not in joined


# -- failure paths ---------------------------------------------------------


def test_a_failing_signal_cli_reports_why(linker, monkeypatch):
    with_mode(monkeypatch, "explode")
    linker.start()
    wait_for(linker, "failed")
    assert "connection refused" in linker.snapshot()["message"]
    assert linker._config.get("signal", "account") == ""


def test_no_uri_within_the_deadline_gives_up(linker, monkeypatch):
    with_mode(monkeypatch, "no-uri")
    monkeypatch.setattr("src.signal_link.URI_TIMEOUT", 1.0)
    linker.start()
    wait_for(linker, "failed", timeout=10)
    assert "did not produce a link" in linker.snapshot()["message"]


def test_an_unapproved_link_expires(linker, monkeypatch):
    with_mode(monkeypatch, "hang")
    monkeypatch.setattr("src.signal_link.LINK_TIMEOUT", 1.5)
    linker.start()
    wait_for(linker, "timeout", timeout=10)
    assert "expired" in linker.snapshot()["message"]
    assert linker.snapshot()["uri"] == ""


def test_cancel_stops_the_process(linker, monkeypatch):
    with_mode(monkeypatch, "hang")
    linker.start()
    wait_for(linker, "waiting")
    assert linker.cancel() is True
    assert linker.snapshot()["phase"] == "cancelled"
    assert linker.snapshot()["uri"] == ""


def test_a_second_start_while_running_is_refused(linker, monkeypatch):
    with_mode(monkeypatch, "hang")
    linker.start()
    wait_for(linker, "waiting")
    started, reason = linker.start()
    assert not started
    assert "already in progress" in reason
    linker.cancel()


def test_starting_when_already_linked_needs_force(linker, monkeypatch):
    with_mode(monkeypatch, "ok")
    linker._config.set(NUMBER, "signal", "account")
    started, reason = linker.start()
    assert not started
    assert "already linked" in reason
    assert linker.start(force=True)[0]
    wait_for(linker, "linked")


def test_a_missing_binary_is_reported_not_crashed(tmp_path):
    linker = SignalLinker(
        Config(tmp_path / "config.json"),
        signal_dir=tmp_path / "signal-cli",
        binary=str(tmp_path / "nope"),
        env_path=tmp_path / "signal.env",
        runner=lambda command: (1, ""),
    )
    started, reason = linker.start()
    assert not started
    assert "not installed" in reason


# -- the account value is about to become a root process argument ----------


def test_a_number_that_is_not_a_number_is_refused(linker):
    """`ExecStart=... -a ${SIGNAL_ACCOUNT}` is word-split by systemd."""
    ok, detail = linker.write_env("; rm -rf /")
    assert not ok
    assert "not a phone number" in detail
    assert not linker.env_path.exists()


def test_writing_the_env_keeps_other_settings(linker):
    linker.env_path.write_text(
        "JAVA_OPTS=-Xmx192m\nSIGNAL_ACCOUNT=+440000000000\n", encoding="utf-8"
    )
    assert linker.write_env(NUMBER)[0]
    text = linker.env_path.read_text()
    assert "JAVA_OPTS=-Xmx192m" in text
    assert f"SIGNAL_ACCOUNT={NUMBER}" in text
    assert "+440000000000" not in text
    assert stat.S_IMODE(os.stat(linker.env_path).st_mode) == 0o640


def test_listaccounts_ignores_a_bad_number(linker, monkeypatch):
    with_mode(monkeypatch, "bad-number")
    assert linker.linked_accounts() == []


# -- unlinking -------------------------------------------------------------


def test_unlink_parks_the_data_and_clears_the_account(linker, monkeypatch):
    with_mode(monkeypatch, "ok")
    linker.start()
    wait_for(linker, "linked")
    (linker.signal_dir / "data").mkdir(parents=True, exist_ok=True)
    (linker.signal_dir / "data" / "account").write_text("secret")

    ok, parked = linker.unlink()
    assert ok
    # Moved aside rather than deleted, so a mistake is recoverable.
    assert parked and os.path.exists(parked)
    assert linker._config.get("signal", "account") == ""
    assert "SIGNAL_ACCOUNT=\n" in linker.env_path.read_text()
    assert [
        "sudo", "-n", "/usr/bin/systemctl", "disable", "--now",
        "signal-cli.service", "little-voicemail.service",
    ] in linker.calls


def test_service_states_do_not_use_sudo(linker):
    """is-active and is-enabled need no privilege."""
    linker.service_states()
    systemctl_calls = [c for c in linker.calls if "systemctl" in " ".join(c)]
    assert systemctl_calls
    assert all("sudo" not in call for call in systemctl_calls)
