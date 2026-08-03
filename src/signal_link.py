"""Linking this device to a Signal account, driven from the web UI.

Linking makes the box a *companion device* of an account that already exists
on a phone, exactly like Signal Desktop. `signal-cli link` prints an
`sgnl://linkdevice?...` URI and then blocks until the phone approves it, so
the work happens in a background thread and the browser polls `snapshot()`.

The parent is very likely holding the phone that owns the account, which is
why the URI matters more than the QR code: on Android, tapping an
`sgnl://linkdevice` link opens Signal straight into the linking prompt, so
the whole thing can be done on one device. Signal on iOS refuses external
provisioning URLs and sends the user to Settings -> Linked devices instead,
where only a camera scan will do - the web UI renders a QR code as well for
that case, but it needs a second screen.

Once the phone approves, the account number has to land in two places: the
config file the services read, and SIGNAL_ACCOUNT in the environment file
signal-cli.service reads. Both are written here, and then the two services
that need an account are enabled.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import SYSTEM_CONFIG_DIR, signal_config_dir

log = logging.getLogger(__name__)

SIGNAL_CLI = "/usr/local/bin/signal-cli"
SYSTEMCTL = "/usr/bin/systemctl"
DEVICE_NAME = "Little Voicemail"

# signal-cli prints the URI almost immediately. Waiting much longer than this
# means the binary or the JRE is broken, and saying so beats a spinner.
URI_TIMEOUT = 45.0
# Provisioning URIs do not stay valid forever. Give the parent five minutes to
# find the phone, then stop and offer a fresh one.
LINK_TIMEOUT = 300.0

LINK_URI = re.compile(r"sgnl://linkdevice\?[^\s\"'<>]+")
E164 = re.compile(r"\+[1-9]\d{6,14}")

SERVICES = ("signal-cli.service", "little-voicemail.service")

ACTIVE_PHASES = ("starting", "waiting")

# How long the child has to go quiet before an unterminated line is taken to
# be all there is. signal-cli prints the URI and then blocks, so waiting for a
# newline that never comes would hang forever.
QUIET_PERIOD = 0.25


@dataclass
class LinkState:
    """Everything the Signal page needs to render itself."""

    phase: str = "idle"  # idle|starting|waiting|linked|failed|cancelled|timeout
    uri: str = ""
    account: str = ""
    message: str = ""
    started_at: float = 0.0
    expires_at: float = 0.0
    log_lines: list[str] = field(default_factory=list)


class SignalLinker:
    """Runs `signal-cli link` and settles the account once it succeeds."""

    def __init__(
        self,
        config,
        signal_dir: Path | None = None,
        binary: str = SIGNAL_CLI,
        env_path: Path | None = None,
        runner=None,
    ):
        self._config = config
        self.signal_dir = Path(signal_dir or signal_config_dir())
        self.binary = binary
        self.env_path = Path(env_path or (SYSTEM_CONFIG_DIR / "signal.env"))
        # Injectable so tests never fork sudo or systemctl.
        self._runner = runner or _run_command
        self._lock = threading.RLock()
        self._state = LinkState()
        self._process: subprocess.Popen | None = None

    # -- introspection ---------------------------------------------------

    @property
    def account(self) -> str:
        return self._config.get("signal", "account", default="") or ""

    @property
    def available(self) -> bool:
        return bool(Path(self.binary).exists() or shutil.which(self.binary))

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._state.phase in ACTIVE_PHASES

    def snapshot(self) -> dict:
        """A fresh JSON-ready dict; never hands out the live dataclass."""
        with self._lock:
            state = self._state
            left = 0
            if state.expires_at and state.phase in ACTIVE_PHASES:
                left = max(0, int(state.expires_at - time.time()))
            return {
                "phase": state.phase,
                "uri": state.uri,
                "account": state.account,
                "message": state.message,
                "seconds_left": left,
                "log": list(state.log_lines[-12:]),
            }

    def service_states(self) -> dict[str, dict]:
        """`is-active` and `is-enabled` need no privilege, so no sudo here."""
        out: dict[str, dict] = {}
        for unit in SERVICES:
            _, active = self._runner([SYSTEMCTL, "is-active", unit])
            _, enabled = self._runner([SYSTEMCTL, "is-enabled", unit])
            out[unit] = {
                "active": active.strip() == "active",
                "enabled": enabled.strip() in ("enabled", "enabled-runtime"),
                "state": active.strip() or "unknown",
            }
        return out

    def linked_accounts(self) -> list[str]:
        """Ask signal-cli which accounts its config directory holds."""
        code, out = self._runner(
            [
                self.binary,
                "--config",
                str(self.signal_dir),
                "--output",
                "json",
                "listAccounts",
            ]
        )
        if code != 0 and not out.strip():
            return []
        try:
            parsed = json.loads(out)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, dict):
            parsed = parsed.get("accounts", [])
        numbers = []
        for entry in parsed if isinstance(parsed, list) else []:
            if isinstance(entry, dict):
                candidate = str(entry.get("number") or entry.get("account") or "")
            else:
                candidate = str(entry)
            if E164.fullmatch(candidate):
                numbers.append(candidate)
        return numbers

    # -- state helpers ---------------------------------------------------

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._state, key, value)

    def _log(self, line: str) -> None:
        # The provisioning URI is a secret: anyone who has it can link
        # themselves to the account. It must never reach journald.
        safe = LINK_URI.sub("sgnl://linkdevice?<hidden>", line).strip()[:200]
        if not safe:
            return
        log.info("signal-link: %s", safe)
        with self._lock:
            self._state.log_lines.append(safe)
            del self._state.log_lines[:-40]

    def _fail(self, message: str) -> None:
        self._log(message)
        self._set(phase="failed", uri="", message=message)

    # -- linking ---------------------------------------------------------

    def start(
        self, device_name: str = DEVICE_NAME, force: bool = False
    ) -> tuple[bool, str]:
        """Begin linking. Returns (started, reason-if-not)."""
        with self._lock:
            if self._state.phase in ACTIVE_PHASES:
                return False, "a link is already in progress"
            if self.account and not force:
                return False, "this device is already linked"
            if not self.available:
                return False, (
                    "signal-cli is not installed. Re-run the installer, or "
                    "flash the prebuilt image."
                )
            now = time.time()
            self._state = LinkState(
                phase="starting",
                message="Asking Signal for a link...",
                started_at=now,
                expires_at=now + LINK_TIMEOUT,
            )
        threading.Thread(
            target=self._run_link, args=(device_name,), daemon=True, name="signal-link"
        ).start()
        return True, ""

    def cancel(self) -> bool:
        with self._lock:
            process = self._process
            was_running = self._state.phase in ACTIVE_PHASES
            if was_running:
                self._state.phase = "cancelled"
                self._state.uri = ""
                self._state.message = "Linking cancelled."
        self._terminate(process)
        return was_running

    def _terminate(self, process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def _run_link(self, device_name: str) -> None:
        # Only one signal-cli may hold the account directory at a time, so the
        # daemon has to be out of the way before `link` opens it.
        self._runner(["sudo", "-n", SYSTEMCTL, "stop", "signal-cli.service"])

        before = set(self.linked_accounts())
        command = [
            self.binary,
            "--config",
            str(self.signal_dir),
            "link",
            "-n",
            device_name,
        ]
        try:
            self.signal_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(f"Could not create {self.signal_dir}: {exc}")
            return

        env = dict(os.environ)
        # The web UI and a JVM together are tight on a 512 MB board.
        env.setdefault("JAVA_OPTS", "-Xmx256m")

        self._log("starting signal-cli link")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                # One pipe, one reader. Two pipes with only one drained can
                # deadlock as soon as a chatty JVM fills the other.
                stderr=subprocess.STDOUT,
                # Bytes, not text: _pump reads the raw fd through select so it
                # can tell "nothing more is coming" from "not finished yet".
                bufsize=0,
                env=env,
            )
        except OSError as exc:
            self._fail(f"Could not run signal-cli: {exc}")
            return

        with self._lock:
            self._process = process

        threading.Thread(
            target=self._watchdog, args=(process,), daemon=True, name="signal-link-wd"
        ).start()

        output = self._pump(process)
        code = process.wait()
        with self._lock:
            self._process = None

        # The watchdog may already have decided this attempt is over, in which
        # case its explanation is the useful one - not the exit code that
        # terminating the child produced.
        if self.snapshot()["phase"] not in ACTIVE_PHASES:
            return
        if code != 0:
            self._fail(_last_useful(output) or f"signal-cli exited with {code}")
            return
        self._settle(before, output)

    def _pump(self, process: subprocess.Popen) -> list[str]:
        """Read the child's output and watch for the provisioning URI.

        Deliberately not `for line in stdout`: signal-cli prints the URI and
        then blocks, with no guarantee that a newline arrives with it, so a
        line-oriented read can sit on a buffered URI forever.

        The other half of the problem is that a partially-read URI still
        matches the pattern - `sgnl://linkdevice?u` is a legal match - so the
        URI is only taken once it is known to be complete: either a newline
        terminated it, or the child has gone quiet for `QUIET_PERIOD`, which
        is exactly what it does while it waits for the phone.
        """
        stream = process.stdout
        if stream is None:
            return []
        fd = stream.fileno()
        buffer = ""
        pending = ""
        lines: list[str] = []
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], QUIET_PERIOD)
                if not ready:
                    # Nothing more is coming for now, so whatever is buffered
                    # is a whole line even without its newline.
                    self._publish_uri(pending)
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                pending = buffer
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if line.strip():
                        lines.append(line.strip())
                        self._log(line)
                    self._publish_uri(line)
                buffer = pending
        except (OSError, ValueError):
            pass
        if pending.strip():
            lines.append(pending.strip())
            self._log(pending)
        return lines

    def _publish_uri(self, text: str) -> None:
        if not text or self.snapshot()["uri"]:
            return
        match = LINK_URI.search(text)
        if not match:
            return
        self._set(
            phase="waiting",
            uri=match.group(0),
            message="Open this link in Signal on the phone that owns the number.",
        )
        self._log("link URI ready")

    def _watchdog(self, process: subprocess.Popen) -> None:
        """Stop waiting on a URI that never comes, or a link nobody approves."""
        uri_deadline = time.time() + URI_TIMEOUT
        while process.poll() is None:
            snapshot = self.snapshot()
            if snapshot["phase"] not in ACTIVE_PHASES:
                return
            if not snapshot["uri"] and time.time() > uri_deadline:
                self._set(
                    phase="failed",
                    message="signal-cli did not produce a link. Check that "
                    "Java is installed, then try again.",
                )
                self._terminate(process)
                return
            with self._lock:
                expires_at = self._state.expires_at
            if expires_at and time.time() > expires_at:
                self._set(
                    phase="timeout",
                    uri="",
                    message="The link expired before the phone approved it. "
                    "Start again for a fresh one.",
                )
                self._terminate(process)
                return
            time.sleep(0.5)

    # -- settling --------------------------------------------------------

    def _settle(self, before: set[str], output: list[str]) -> None:
        """The phone approved. Record the number and start the services."""
        account = self._resolve_account(before, output)
        if not account:
            self._fail(
                "Signal accepted the link but did not report a phone number. "
                "Reload the Signal page and try again."
            )
            return

        self._log(f"linked as {account}")
        self._set(
            phase="linked",
            uri="",
            account=account,
            message=f"Linked to {account}. Starting the phone service...",
        )
        self._persist(account)

        ok, detail = self._systemctl_units("enable", "--now")
        if ok:
            self._set(
                message=f"Linked to {account}. The phone service is starting - "
                f"it takes half a minute to connect."
            )
        else:
            self._set(
                message=f"Linked to {account}, but the services did not start: "
                f"{detail}"
            )

    def _resolve_account(self, before: set[str], output: list[str]) -> str:
        after = set(self.linked_accounts())
        new = after - before
        if len(new) == 1:
            return new.pop()
        if after:
            return sorted(after)[-1]
        # Older signal-cli builds print "Associated with: +44..." and nothing
        # machine-readable; scrape the link output rather than fail outright.
        for line in reversed(output):
            match = E164.search(line)
            if match:
                return match.group(0)
        return ""

    def _persist(self, account: str) -> None:
        self._config.set(account, "signal", "account")
        ok, detail = self.write_env(account)
        if not ok:
            self._log(f"could not write {self.env_path}: {detail}")

    # -- account plumbing ------------------------------------------------

    def write_env(self, account: str) -> tuple[bool, str]:
        """Put SIGNAL_ACCOUNT into the file signal-cli.service reads.

        The number lives in two places - here and in the config file - and
        they have to agree, or the daemon starts against the wrong account.

        The value is validated first. systemd word-splits `${SIGNAL_ACCOUNT}`
        in `ExecStart=`, so anything that is not a bare phone number would be
        extra arguments to a root-installed unit.
        """
        if account and not E164.fullmatch(account):
            return False, f"{account!r} is not a phone number"
        try:
            existing: list[str] = []
            if self.env_path.exists():
                existing = self.env_path.read_text(encoding="utf-8").splitlines()
            lines = [
                line for line in existing if not line.startswith("SIGNAL_ACCOUNT=")
            ]
            lines.append(f"SIGNAL_ACCOUNT={account}")
            self.env_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.env_path.parent), prefix=".signal-env-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.env_path)
            self.env_path.chmod(0o640)
            return True, ""
        except OSError as exc:
            return False, str(exc)

    def unlink(self) -> tuple[bool, str]:
        """Forget the account and stop the services that need one.

        The signal-cli state is moved aside rather than deleted: if this was a
        mistake, the directory can be moved back. It does not remove the
        device from the phone - only Signal itself can do that.
        """
        self.cancel()
        self._systemctl_units("disable", "--now")

        parked = ""
        if self.signal_dir.exists() and any(self.signal_dir.iterdir()):
            parked = f"{self.signal_dir}.unlinked-{int(time.time())}"
            try:
                shutil.move(str(self.signal_dir), parked)
            except OSError as exc:
                return False, f"could not move the Signal data aside: {exc}"
        try:
            self.signal_dir.mkdir(parents=True, exist_ok=True)
            self.signal_dir.chmod(0o700)
        except OSError:
            pass

        self._config.set("", "signal", "account")
        self.write_env("")
        with self._lock:
            self._state = LinkState(message="This device is no longer linked.")
        self._log(f"unlinked; previous data at {parked or '(none)'}")
        return True, parked

    # -- services --------------------------------------------------------

    def _systemctl_units(self, verb: str, flag: str) -> tuple[bool, str]:
        """Run one of the exact command lines allowed in sudoers.

        sudo matches the whole argument vector, so these have to be assembled
        the same way every time and stay in step with
        /etc/sudoers.d/little-voicemail.
        """
        code, out = self._runner(
            ["sudo", "-n", SYSTEMCTL, verb, flag, *SERVICES]
        )
        detail = out.strip()
        if detail:
            self._log(detail[-300:])
        return code == 0, detail


def _run_command(command) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(command), capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _last_useful(lines: list[str]) -> str:
    for line in reversed(lines):
        stripped = LINK_URI.sub("", line).strip()
        if stripped:
            return stripped[:200]
    return ""
