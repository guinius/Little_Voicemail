"""Over-the-air updates from the GitHub repository.

The web UI asks `check()` on load: it compares the checked-out commit with
the tip of the configured branch and reports both version strings so the
page can offer an Update button.

Applying an update is a git fast-forward plus a dependency install, then a
reboot. The work happens in a detached child process so the HTTP response
gets back to the parent's browser before the service goes down; if the
update fails the checkout is rolled back to the commit it started from,
which matters when the box lives in a child's bedroom and nobody is going
to SSH in and fix it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PROJECT_ROOT

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
CHECK_TIMEOUT = 8
CACHE_SECONDS = 300


@dataclass
class UpdateStatus:
    current_version: str = "unknown"
    current_commit: str = ""
    latest_version: str = ""
    latest_commit: str = ""
    update_available: bool = False
    checked_at: float = 0.0
    error: str = ""
    detail: str = ""


@dataclass
class UpdateProgress:
    running: bool = False
    finished: bool = False
    ok: bool = False
    message: str = ""
    log_lines: list[str] = field(default_factory=list)


class Updater:
    def __init__(self, config, repo_root: Path | None = None):
        self._config = config
        self.root = Path(repo_root or PROJECT_ROOT)
        self._cache: UpdateStatus | None = None
        self._lock = threading.Lock()
        self.progress = UpdateProgress()

    # -- identity --------------------------------------------------------

    @property
    def repo(self) -> str:
        return self._config.get(
            "updates", "repo", default="guinius/Little_Voicemail"
        )

    @property
    def branch(self) -> str:
        return self._config.get("updates", "branch", default="master")

    def local_version(self) -> str:
        version_file = self.root / "VERSION"
        if version_file.exists():
            return version_file.read_text(encoding="utf-8").strip()
        return "unknown"

    def local_commit(self) -> str:
        ok, out = self._git("rev-parse", "HEAD")
        return out.strip()[:8] if ok else ""

    def is_git_checkout(self) -> bool:
        return (self.root / ".git").exists()

    # -- checking --------------------------------------------------------

    def check(self, force: bool = False) -> UpdateStatus:
        with self._lock:
            cached = self._cache
            if (
                not force
                and cached is not None
                and (time.time() - cached.checked_at) < CACHE_SECONDS
            ):
                return cached
            status = self._check_uncached()
            self._cache = status
            return status

    def _check_uncached(self) -> UpdateStatus:
        status = UpdateStatus(
            current_version=self.local_version(),
            current_commit=self.local_commit(),
            checked_at=time.time(),
        )
        if not self.is_git_checkout():
            status.error = "Not a git checkout; updates are unavailable."
            return status
        try:
            head = self._fetch_json(
                f"{GITHUB_API}/repos/{self.repo}/commits/{self.branch}"
            )
            status.latest_commit = str(head.get("sha", ""))[:8]
            remote_version = self._fetch_remote_version()
            status.latest_version = remote_version or status.latest_commit
            status.update_available = bool(
                status.latest_commit
                and status.current_commit
                and status.latest_commit != status.current_commit
            )
            if status.update_available:
                message = ((head.get("commit") or {}).get("message") or "").strip()
                status.detail = message.splitlines()[0][:120] if message else ""
        except urllib.error.HTTPError as exc:
            status.error = (
                "Repository not reachable (HTTP %s). A private repo needs a "
                "token in the update settings." % exc.code
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status.error = f"Could not reach GitHub: {exc}"
        except (ValueError, KeyError) as exc:
            status.error = f"Unexpected response from GitHub: {exc}"
        return status

    def _fetch_remote_version(self) -> str:
        url = (
            f"https://raw.githubusercontent.com/{self.repo}/"
            f"{self.branch}/VERSION"
        )
        try:
            with urllib.request.urlopen(url, timeout=CHECK_TIMEOUT) as resp:
                return resp.read().decode("utf-8").strip()
        except (urllib.error.URLError, OSError):
            return ""

    def _fetch_json(self, url: str) -> dict:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "little-voicemail-updater",
            },
        )
        token = self._config.get("updates", "token", default="")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- applying --------------------------------------------------------

    def start_update(self, reboot: bool = True) -> bool:
        """Kick off the update in a background thread. False if already running."""
        with self._lock:
            if self.progress.running:
                return False
            self.progress = UpdateProgress(running=True, message="Starting update...")
        thread = threading.Thread(
            target=self._run_update, args=(reboot,), daemon=True, name="updater"
        )
        thread.start()
        return True

    def _run_update(self, reboot: bool) -> None:
        rollback_to = self.local_commit()
        try:
            self._step("Fetching latest changes", "git", "fetch", "--all", "--prune")
            self._step(
                "Applying update",
                "git", "reset", "--hard", f"origin/{self.branch}",
            )
            requirements = self.root / "requirements.txt"
            if requirements.exists():
                self._step(
                    "Installing dependencies",
                    self._pip(), "install", "--upgrade", "-r", str(requirements),
                )
            with self._lock:
                self.progress.ok = True
                self.progress.message = (
                    "Update installed. Rebooting..." if reboot
                    else "Update installed."
                )
                self._cache = None
        except UpdateFailed as exc:
            log.error("update failed: %s", exc)
            if rollback_to:
                self._log(f"Rolling back to {rollback_to}")
                self._git("reset", "--hard", rollback_to)
            with self._lock:
                self.progress.ok = False
                self.progress.message = f"Update failed: {exc}"
        finally:
            with self._lock:
                self.progress.running = False
                self.progress.finished = True

        if self.progress.ok and reboot:
            # Give the browser a moment to collect the final status.
            time.sleep(3)
            self.reboot()

    def _step(self, label: str, *command: str) -> None:
        self._log(label)
        with self._lock:
            self.progress.message = label
        ok, output = self._run(*command)
        if output.strip():
            self._log(output.strip()[-500:])
        if not ok:
            raise UpdateFailed(label.lower())

    def _pip(self) -> str:
        venv_pip = self.root / ".venv/bin/pip"
        return str(venv_pip) if venv_pip.exists() else "pip3"

    def _git(self, *args: str) -> tuple[bool, str]:
        return self._run("git", *args)

    def _run(self, *command: str) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            return result.returncode == 0, (result.stdout + result.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)

    def _log(self, line: str) -> None:
        log.info("updater: %s", line)
        with self._lock:
            self.progress.log_lines.append(line)
            del self.progress.log_lines[:-40]

    @staticmethod
    def reboot() -> None:
        log.warning("rebooting to complete update")
        subprocess.run(["sudo", "/sbin/reboot"], check=False)


class UpdateFailed(RuntimeError):
    pass
