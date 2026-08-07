"""AudioEngine._reapply_levels() and _kick_off_levels_reapply().

Regression coverage for two things:

  * the codec resetting its own playback/capture levels after being idle:
    set-audio-levels.sh has to run before every real recording/playback,
    not just once at boot. See audio.py's LEVELS_SCRIPT docstring and
    tools/set-audio-levels.sh.
  * a real bug that fix introduced: awaiting that script inline blocked
    the PTT lamp lighting and the actual arecord launch for however long
    the script took (a real 1-3s). A normal quick press-and-release
    finished before start_recording() had even returned, so the release
    fired the instant the delayed start finally completed, stopping a
    recording milliseconds old - discarded as too short, with no lamp
    ever having lit. Looked exactly like a dead button. Fixed by
    backgrounding the reapply instead of awaiting it - see
    _kick_off_levels_reapply().
"""

import asyncio
import gc
import time

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


# -- _kick_off_levels_reapply() ---------------------------------------------


@pytest.mark.asyncio
async def test_kick_off_levels_reapply_returns_immediately(tmp_path, monkeypatch):
    """The actual regression: a slow script must not delay the caller -
    start_recording()/play() need the lamp/state transition and the real
    arecord/ffplay launch to happen right away, not after the script
    finishes. asyncio.create_task() needs a running loop, hence async, but
    nothing here is awaited - the point is that _kick_off_levels_reapply()
    itself is a synchronous, immediate call."""
    script = tmp_path / "fake-levels.sh"
    script.write_text("#!/bin/sh\nsleep 2\n")
    script.chmod(0o755)
    monkeypatch.setattr(audio_module, "LEVELS_SCRIPT", script)
    engine = make_engine(tmp_path)

    started = time.monotonic()
    engine._kick_off_levels_reapply()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    engine._levels_task.cancel()  # don't leave the 2s sleep running past the test


@pytest.mark.asyncio
async def test_kick_off_levels_reapply_still_runs_the_script(tmp_path, monkeypatch):
    """Backgrounded, not abandoned - it has to actually complete, just not
    be waited on by the caller."""
    script = tmp_path / "fake-levels.sh"
    marker = tmp_path / "ran"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    monkeypatch.setattr(audio_module, "LEVELS_SCRIPT", script)
    engine = make_engine(tmp_path)

    engine._kick_off_levels_reapply()
    await engine._levels_task

    assert marker.exists()


@pytest.mark.asyncio
async def test_kick_off_levels_reapply_is_not_garbage_collected(tmp_path, monkeypatch):
    """asyncio only keeps a weak reference to a bare create_task() result -
    without _levels_task holding a strong one, gc could reap the task
    before the script even runs."""
    script = tmp_path / "fake-levels.sh"
    marker = tmp_path / "ran"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    monkeypatch.setattr(audio_module, "LEVELS_SCRIPT", script)
    engine = make_engine(tmp_path)

    engine._kick_off_levels_reapply()
    gc.collect()
    await asyncio.sleep(0.2)

    assert marker.exists()
