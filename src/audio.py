"""Recording, encoding and playback.

Recording uses `arecord` straight to WAV, then ffmpeg transcodes to mono
AAC in an M4A container at 48 kbps - the format both Signal's iOS and
Android apps actually record their own voice notes in. Ogg/Opus looks like
the more obvious choice (lower bitrate, Signal's own docs mention it,
Android and Desktop play it fine) and was tried first, but Signal iOS has a
longstanding, unresolved bug where Opus voice attachments from other
platforms just don't play (signalapp/Signal-iOS#5771) - the recording shows
up but tapping it does nothing. AAC/M4A is what actually round-trips to
every client, which matters more here than the extra bitrate costs: a
minute of speech is still well under a megabyte.

Playback goes through ffplay so that whatever a parent's phone sends -
AAC from iOS, Opus from Android, m4a from Signal Desktop - just works.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import PROJECT_ROOT

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
AAC_BITRATE = "48k"
# See tools/set-audio-levels.sh. The ReSpeaker codec doesn't just default to
# quiet at boot - it appears to reset its own playback/capture volume
# registers back to those defaults whenever its analog stage powers back up
# after being idle (a DAPM power-management pattern common to ASoC codecs),
# so a level fixed once at boot can go quietly missing again hours into
# real use. Cheapest reliable fix is reapplying it around every actual use
# rather than chasing the exact codec behaviour that causes it.
LEVELS_SCRIPT = PROJECT_ROOT / "tools" / "set-audio-levels.sh"


class AudioError(RuntimeError):
    pass


@dataclass
class Recording:
    path: Path
    duration: float
    aborted: bool = False


class AudioEngine:
    def __init__(self, config, work_dir: Path, sounds_dir: Path):
        self._config = config
        self.work_dir = Path(work_dir)
        self.sounds_dir = Path(sounds_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._record_proc: asyncio.subprocess.Process | None = None
        self._playback_proc: asyncio.subprocess.Process | None = None
        self._play_lock = asyncio.Lock()

    # -- settings --------------------------------------------------------

    @property
    def input_device(self) -> str:
        return self._config.get("audio", "input_device", default="default")

    @property
    def output_device(self) -> str:
        return self._config.get("audio", "output_device", default="default")

    @property
    def max_record_seconds(self) -> int:
        return int(self._config.get("audio", "max_record_seconds", default=60))

    @property
    def min_record_seconds(self) -> float:
        return float(self._config.get("audio", "min_record_seconds", default=0.7))

    # -- levels ------------------------------------------------------------

    async def _reapply_levels(self) -> None:
        """Best-effort re-run of set-audio-levels.sh before touching real
        hardware. Never raises: a failure here should not stop a recording
        or a playback from being attempted, just leave the mic/speaker at
        whatever level the codec happened to reset itself to."""
        if not LEVELS_SCRIPT.exists():
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                str(LEVELS_SCRIPT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            log.warning("could not reapply audio levels: %s", exc)
            return
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            # Don't leave it running - a script that hangs once is liable to
            # hang every time, and this runs before every recording/
            # playback, so a leaked process here would pile up fast.
            proc.kill()
            try:
                # kill() alone isn't enough of a guarantee here: a process
                # with stdout=PIPE that communicate() never finished
                # draining can leave Process.wait() hanging indefinitely
                # even after the kill, regardless of the process actually
                # being dead - bound this wait too rather than trust it to
                # return promptly.
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass  # best-effort cleanup; not worth blocking on further
            log.warning("set-audio-levels.sh timed out; killed it")
            return
        if proc.returncode != 0:
            log.warning(
                "set-audio-levels.sh exited %s: %s",
                proc.returncode, out.decode(errors="replace").strip()[-300:],
            )

    # -- recording -------------------------------------------------------

    async def start_recording(self) -> Path:
        """Begin capture. Returns the WAV path being written."""
        if self._record_proc is not None:
            raise AudioError("a recording is already running")
        _require("arecord")
        await self._reapply_levels()
        target = self.work_dir / f"rec-{int(time.time() * 1000)}.wav"
        # -d caps the capture so a jammed button cannot record forever
        # (requirement 4); arecord exits cleanly on its own at the limit.
        self._record_proc = await asyncio.create_subprocess_exec(
            "arecord",
            "-D", self.input_device,
            "-f", "S16_LE",
            "-r", str(SAMPLE_RATE),
            "-c", "1",
            "-d", str(self.max_record_seconds),
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._recording_started = time.monotonic()
        self._recording_path = target
        log.info("recording to %s", target)
        return target

    async def stop_recording(self) -> Recording:
        """End capture and return the raw WAV plus its duration.

        A press too short to be speech is reported as aborted so the caller
        can discard it instead of sending a click to Grandma.
        """
        proc = self._record_proc
        if proc is None:
            raise AudioError("no recording in progress")
        self._record_proc = None
        duration = time.monotonic() - self._recording_started
        if proc.returncode is None:
            proc.terminate()
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stderr = b""
        path = self._recording_path
        if stderr:
            log.debug("arecord: %s", stderr.decode(errors="replace").strip())
        aborted = duration < self.min_record_seconds or not path.exists()
        if aborted:
            log.info("discarding %.2fs recording (too short)", duration)
            _unlink(path)
        return Recording(path=path, duration=duration, aborted=aborted)

    async def recording_elapsed(self) -> float:
        if self._record_proc is None:
            return 0.0
        return time.monotonic() - self._recording_started

    @property
    def is_recording(self) -> bool:
        return self._record_proc is not None

    # -- encoding --------------------------------------------------------

    async def encode_voice_note(self, wav_path: Path) -> Path:
        """Transcode a captured WAV to AAC/M4A for sending as a voice note."""
        _require("ffmpeg")
        target = wav_path.with_suffix(".m4a")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-y",
            "-i", str(wav_path),
            "-ac", "1",
            # A far-field mic picking up a child at an unpredictable
            # distance produces uneven levels - dynaudnorm adaptively boosts
            # quiet stretches frame by frame instead of one flat gain, which
            # would either leave quiet parts quiet or clip the loud ones.
            "-af", "dynaudnorm",
            "-c:a", "aac",
            "-b:a", AAC_BITRATE,
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not target.exists():
            raise AudioError(
                f"aac encode failed: {stderr.decode(errors='replace')[-400:]}"
            )
        _unlink(wav_path)
        return target

    # -- playback --------------------------------------------------------

    async def play(self, path: str | Path, volume: float = 1.0) -> bool:
        """Play one file to completion. Returns False if it could not play."""
        path = Path(path)
        if not path.exists():
            log.warning("cannot play missing file %s", path)
            return False
        if not shutil.which("ffplay"):
            log.error("ffplay not installed; cannot play audio")
            return False
        async with self._play_lock:
            await self._reapply_levels()
            proc = await asyncio.create_subprocess_exec(
                "ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
                "-volume", str(int(max(0.0, min(1.0, volume)) * 100)),
                str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_playback_env(self.output_device),
            )
            self._playback_proc = proc
            try:
                _, stderr = await proc.communicate()
            finally:
                self._playback_proc = None
            if proc.returncode != 0:
                log.warning(
                    "playback of %s failed: %s",
                    path, stderr.decode(errors="replace").strip(),
                )
                return False
        return True

    async def play_sequence(self, paths, gap: float | None = None,
                            on_played=None) -> int:
        """Play several messages back to back with a gap between them.

        `on_played` is awaited after each file so the caller can retire that
        message from the queue as it finishes, not all at the end - if the
        power goes out halfway through, the child does not hear the first
        three again.
        """
        if gap is None:
            gap = float(self._config.get("audio", "playback_gap_seconds", default=1.0))
        played = 0
        for index, item in enumerate(paths):
            if index:
                await asyncio.sleep(gap)
            path = item.attachment if hasattr(item, "attachment") else item
            if await self.play(path):
                played += 1
            if on_played is not None:
                await on_played(item)
        return played

    async def play_ringtone(self) -> None:
        name = self._config.get("audio", "ringtone", default="chime.wav")
        volume = float(self._config.get("audio", "ringtone_volume", default=0.8))
        path = self.sounds_dir / name
        if not path.exists():
            log.warning("ringtone %s missing; falling back to first available", name)
            available = sorted(self.sounds_dir.glob("*.wav"))
            if not available:
                return
            path = available[0]
        await self.play(path, volume=volume)

    def available_ringtones(self) -> list[str]:
        if not self.sounds_dir.exists():
            return []
        return sorted(
            p.name
            for p in self.sounds_dir.iterdir()
            if p.suffix.lower() in {".wav", ".mp3", ".ogg"}
        )

    async def stop_playback(self) -> None:
        proc = self._playback_proc
        if proc is not None and proc.returncode is None:
            proc.terminate()

    # -- housekeeping ----------------------------------------------------

    def cleanup_work_dir(self, keep_seconds: int = 86400) -> int:
        """Delete stale recordings left behind by crashes."""
        cutoff = time.time() - keep_seconds
        removed = 0
        for path in self.work_dir.glob("rec-*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def _playback_env(device: str) -> dict | None:
    """Point ffplay's ALSA output at the configured device."""
    import os

    if not device or device == "default":
        return None
    env = dict(os.environ)
    env["SDL_AUDIODRIVER"] = "alsa"
    env["AUDIODEV"] = device
    env["ALSA_CARD"] = device
    return env


def _require(binary: str) -> None:
    if not shutil.which(binary):
        raise AudioError(f"{binary} is not installed")


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
