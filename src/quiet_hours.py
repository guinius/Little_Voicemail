"""Quiet-time windows.

Up to three independently toggleable windows (school, nap, bedtime). While
any enabled window is active the device is inert: no ringtone, no LEDs, and
any button press just flashes all six lights three times.

Windows may wrap past midnight ("19:00" -> "07:00"), which is the normal
case for bedtime, so containment is tested on the wrapped interval rather
than a naive start <= now <= end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable


def parse_hhmm(value: str) -> time:
    """Parse 'HH:MM' into a time, tolerating '9:05' and stray whitespace."""
    hour_str, _, minute_str = str(value).strip().partition(":")
    hour, minute = int(hour_str), int(minute_str or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time of day: {value!r}")
    return time(hour=hour, minute=minute)


@dataclass(frozen=True)
class QuietWindow:
    id: str
    label: str
    enabled: bool
    start: time
    end: time
    days: frozenset[int]  # 0 = Monday .. 6 = Sunday

    @property
    def wraps_midnight(self) -> bool:
        return self.end <= self.start

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QuietWindow":
        days = raw.get("days")
        if not days:
            days = list(range(7))
        return cls(
            id=str(raw.get("id", "")),
            label=str(raw.get("label", raw.get("id", ""))),
            enabled=bool(raw.get("enabled", False)),
            start=parse_hhmm(raw.get("start", "00:00")),
            end=parse_hhmm(raw.get("end", "00:00")),
            days=frozenset(int(d) for d in days),
        )

    def contains(self, moment: datetime) -> bool:
        """Is `moment` inside this window?

        For a window that wraps midnight the day-of-week test applies to the
        day the window *started* on, so a bedtime window enabled for Friday
        covers Friday 19:00 through Saturday 07:00.
        """
        if not self.enabled:
            return False
        clock = moment.time()
        if self.wraps_midnight:
            if clock >= self.start:
                return moment.weekday() in self.days
            if clock < self.end:
                # We are in the tail that began yesterday.
                return (moment - timedelta(days=1)).weekday() in self.days
            return False
        return self.start <= clock < self.end and moment.weekday() in self.days

    def next_end_after(self, moment: datetime) -> datetime | None:
        """When does this window next stop containing `moment`?"""
        if not self.contains(moment):
            return None
        end_today = datetime.combine(moment.date(), self.end, tzinfo=moment.tzinfo)
        if self.wraps_midnight and moment.time() >= self.start:
            return end_today + timedelta(days=1)
        return end_today


class QuietHours:
    """Evaluates the configured windows against the current local time."""

    def __init__(self, config):
        self._config = config

    def windows(self) -> list[QuietWindow]:
        raw: Iterable[dict] = self._config.get("quiet_times", default=[]) or []
        out = []
        for entry in raw:
            try:
                out.append(QuietWindow.from_dict(entry))
            except (ValueError, TypeError):
                # A malformed window is skipped rather than taking the
                # device down - worst case quiet time doesn't apply.
                continue
        return out

    def active_window(self, moment: datetime | None = None) -> QuietWindow | None:
        now = moment or datetime.now()
        for window in self.windows():
            if window.contains(now):
                return window
        return None

    def is_quiet(self, moment: datetime | None = None) -> bool:
        return self.active_window(moment) is not None

    def quiet_until(self, moment: datetime | None = None) -> datetime | None:
        """Latest end time across all windows currently containing `moment`.

        Overlapping windows (a nap inside school hours) should keep the
        device quiet until the last of them ends, not the first.
        """
        now = moment or datetime.now()
        ends = [
            end
            for window in self.windows()
            if (end := window.next_end_after(now)) is not None
        ]
        return max(ends) if ends else None
