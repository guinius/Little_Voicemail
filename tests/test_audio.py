"""AudioEngine._reapply_levels().

Regression coverage for the codec resetting its own playback/capture
levels after being idle: set-audio-levels.sh has to run before every real
recording/playback, not just once at boot. See audio.py's LEVELS_SCRIPT
docstring and tools/set-audio-levels.sh.
"""

import pytest

import src.audio as audio_module
from src.audio import AudioEngine


def make_engine(tmp_path):
    return AudioEngine(
        config=None, work_dir=tmp_path / "work", sounds_dir=tmp_path / "sounds"
    )


@pytest.mark.asyncio
async def test_reapply_levels_runs_the_script(tmp_path, monkeypatch):
    script = tmp_path / "fake-levels.sh"
    marker = tmp_path / "ran"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    monkeypatch.setattr(audio_module, "LEVELS_SCRIPT", script)

    await make_engine(tmp_path)._reapply_levels()

    assert marker.exists()


@pytest.mark.asyncio
async def test_reapply_levels_does_not_raise_on_a_failing_script(tmp_path, monkeypatch):
    script = tmp_path / "fake-levels.sh"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr(audio_module, "LEVELS_SCRIPT", script)

    await make_engine(tmp_path)._reapply_levels()  # must not raise


@pytest.mark.asyncio
async def test_reapply_levels_is_a_noop_when_the_script_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_module, "LEVELS_SCRIPT", tmp_path / "does-not-exist.sh")

    await make_engine(tmp_path)._reapply_levels()  # must not raise


@pytest.mark.asyncio
async def test_reapply_levels_does_not_hang_forever_on_a_stuck_script(tmp_path, monkeypatch):
    # Expect a benign PytestUnraisableExceptionWarning from this one: the
    # killed-but-still-draining child's transport gets garbage collected a
    # beat after this test's own (short-lived, per-test) event loop closes.
    # It's an artifact of pytest-asyncio's per-test loop teardown timing,
    # not something the app's actual long-lived loop would ever hit - not
    # worth chasing further since it doesn't fail the test either way.
    script = tmp_path / "fake-levels.sh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(audio_module, "LEVELS_SCRIPT", script)

    # A stuck codec-level script must not block a recording/playback attempt
    # forever - _reapply_levels() bounds it with its own 5s asyncio.wait_for,
    # well inside this test's own timeout.
    await make_engine(tmp_path)._reapply_levels()
