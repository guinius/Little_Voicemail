"""Factory reset: wiping settings and forgetting WiFi (GitHub issue #19)."""

from __future__ import annotations

import subprocess

from src import factory_reset


def test_wipe_removes_the_config_file(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    factory_reset.wipe(config_path, data_dir)

    assert not config_path.exists()


def test_wipe_removes_a_broken_config_backup_too(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    (tmp_path / "config.json.broken").write_text("oops", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    factory_reset.wipe(config_path, data_dir)

    assert not (tmp_path / "config.json.broken").exists()


def test_wipe_clears_the_data_dir_but_keeps_certs(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "messages.db").write_bytes(b"x")
    (data_dir / "status.json").write_text("{}", encoding="utf-8")
    (data_dir / "session.key").write_bytes(b"secret")
    recordings = data_dir / "recordings"
    recordings.mkdir()
    (recordings / "rec-1.wav").write_bytes(b"x")
    certs = data_dir / "certs"
    certs.mkdir()
    (certs / "server.crt").write_text("cert", encoding="utf-8")

    factory_reset.wipe(config_path, data_dir)

    assert not (data_dir / "messages.db").exists()
    assert not (data_dir / "status.json").exists()
    assert not (data_dir / "session.key").exists()
    assert not recordings.exists()
    # The HTTPS certificate isn't a parent-visible setting - it regenerates
    # itself automatically and wiping it here would just be pure churn.
    assert (certs / "server.crt").exists()


def test_wipe_moves_aside_nothing_that_does_not_exist(tmp_path):
    """A factory reset before anything has ever been saved must not raise."""
    config_path = tmp_path / "nonexistent" / "config.json"
    data_dir = tmp_path / "also-nonexistent"

    factory_reset.wipe(config_path, data_dir)  # must not raise


def test_wipe_removes_the_signal_cli_state(tmp_path, monkeypatch):
    signal_dir = tmp_path / "signal-cli"
    signal_dir.mkdir()
    (signal_dir / "account.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(factory_reset, "signal_config_dir", lambda: signal_dir)
    monkeypatch.setattr(factory_reset.subprocess, "run", lambda *a, **k: _Completed())

    factory_reset.wipe(tmp_path / "config.json", tmp_path / "data")

    assert not signal_dir.exists()


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def test_forget_wifi_calls_the_netctl_helper(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _Completed()

    monkeypatch.setattr(factory_reset.subprocess, "run", fake_run)

    factory_reset.forget_wifi()

    assert captured["command"] == ["sudo", "-n", factory_reset.NETCTL, "forget"]


def test_forget_wifi_does_not_raise_when_the_helper_is_missing(monkeypatch):
    def fake_run(command, **kwargs):
        raise FileNotFoundError("no sudo here")

    monkeypatch.setattr(factory_reset.subprocess, "run", fake_run)

    factory_reset.forget_wifi()  # must not raise


def test_reboot_reports_success(monkeypatch):
    monkeypatch.setattr(
        factory_reset.subprocess, "run",
        lambda *a, **k: _Completed(),
    )

    ok, detail = factory_reset.reboot()

    assert ok is True


def test_reboot_reports_failure_without_raising(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="reboot", timeout=15)

    monkeypatch.setattr(factory_reset.subprocess, "run", fake_run)

    ok, detail = factory_reset.reboot()

    assert ok is False
