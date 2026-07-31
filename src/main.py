"""Entry point for the Little Voicemail device service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal as signal_module
import sys
from pathlib import Path

from .app import PhoneApp
from .audio import AudioEngine
from .config import Config
from .hardware import Hardware
from .messages import MessageQueue
from .paths import (
    default_config_path,
    default_data_dir,
    default_sounds_dir,
    signal_attachment_dir,
)
from .signal_client import SignalClient

log = logging.getLogger("little_voicemail")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="little-voicemail")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--sounds-dir", type=Path, default=default_sounds_dir())
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LV_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def run(args: argparse.Namespace) -> int:
    config = Config(args.config)
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    account = config.get("signal", "account", default="")
    if not account:
        log.error(
            "No Signal account configured. Link this device from the web UI "
            "before starting the phone service."
        )
        return 2

    hardware = Hardware.create()
    if not hardware.live:
        log.warning("running without real GPIO - buttons and LEDs are simulated")

    audio = AudioEngine(
        config, work_dir=data_dir / "recordings", sounds_dir=Path(args.sounds_dir)
    )
    audio.cleanup_work_dir()

    queue = MessageQueue(data_dir / "messages.db")
    queue.prune()

    client = SignalClient(
        account=account,
        host=config.get("signal", "jsonrpc_host", default="127.0.0.1"),
        port=int(config.get("signal", "jsonrpc_port", default=7583)),
        attachment_dir=signal_attachment_dir(),
    )

    app = PhoneApp(
        config, hardware, audio, client, queue, status_path=data_dir / "status.json"
    )

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    runner = asyncio.create_task(app.run(), name="phone-app")
    stopper = asyncio.create_task(stopping.wait(), name="signal-wait")

    log.info("Little Voicemail is running")
    done, pending = await asyncio.wait(
        {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()

    if runner in done and not runner.cancelled():
        exc = runner.exception()
        if exc:
            log.error("phone app stopped with an error", exc_info=exc)
            await app.shutdown()
            queue.close()
            return 1

    log.info("shutting down")
    await app.shutdown()
    queue.close()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.log_level)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
