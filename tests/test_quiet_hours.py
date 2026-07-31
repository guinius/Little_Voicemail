from datetime import datetime

import pytest

from src.quiet_hours import QuietHours, QuietWindow, parse_hhmm


class FakeConfig:
    def __init__(self, windows):
        self._windows = windows

    def get(self, *keys, default=None):
        if keys == ("quiet_times",):
            return self._windows
        return default


def window(**kwargs):
    base = {
        "id": "test",
        "label": "Test",
        "enabled": True,
        "start": "09:00",
        "end": "15:00",
        "days": list(range(7)),
    }
    base.update(kwargs)
    return base


# 2026-07-27 is a Monday.
MON = "2026-07-27"
SAT = "2026-08-01"


def at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}")


def test_parse_hhmm_tolerates_single_digit_hour():
    assert parse_hhmm("9:05").hour == 9
    assert parse_hhmm(" 09:05 ").minute == 5


@pytest.mark.parametrize("bad", ["24:00", "12:60", "abc", "-1:00"])
def test_parse_hhmm_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        parse_hhmm(bad)


def test_simple_window_contains_only_its_span():
    quiet = QuietHours(FakeConfig([window(start="09:00", end="15:00")]))
    assert not quiet.is_quiet(at(MON, "08:59"))
    assert quiet.is_quiet(at(MON, "09:00"))
    assert quiet.is_quiet(at(MON, "14:59"))
    assert not quiet.is_quiet(at(MON, "15:00"))  # end is exclusive


def test_disabled_window_never_matches():
    quiet = QuietHours(FakeConfig([window(enabled=False)]))
    assert not quiet.is_quiet(at(MON, "12:00"))


def test_bedtime_window_wraps_past_midnight():
    quiet = QuietHours(FakeConfig([window(id="bed", start="19:00", end="07:00")]))
    assert quiet.is_quiet(at(MON, "19:00"))
    assert quiet.is_quiet(at(MON, "23:59"))
    assert quiet.is_quiet(at(MON, "00:30"))
    assert quiet.is_quiet(at(MON, "06:59"))
    assert not quiet.is_quiet(at(MON, "07:00"))
    assert not quiet.is_quiet(at(MON, "12:00"))


def test_wrapping_window_attributes_the_tail_to_the_starting_day():
    """A Friday-only bedtime must cover Saturday morning, not Saturday night."""
    friday_only = window(id="bed", start="19:00", end="07:00", days=[4])
    quiet = QuietHours(FakeConfig([friday_only]))

    friday, saturday = "2026-07-31", "2026-08-01"
    assert quiet.is_quiet(at(friday, "20:00"))      # Friday evening
    assert quiet.is_quiet(at(saturday, "06:00"))    # tail of the Friday window
    assert not quiet.is_quiet(at(saturday, "20:00"))  # Saturday evening is not set


def test_school_window_respects_weekdays():
    school = window(id="school", start="09:00", end="15:15", days=[0, 1, 2, 3, 4])
    quiet = QuietHours(FakeConfig([school]))
    assert quiet.is_quiet(at(MON, "10:00"))
    assert not quiet.is_quiet(at(SAT, "10:00"))


def test_active_window_reports_which_one():
    quiet = QuietHours(
        FakeConfig(
            [
                window(id="school", label="School", start="09:00", end="15:15"),
                window(id="nap", label="Nap", start="13:00", end="14:30"),
            ]
        )
    )
    active = quiet.active_window(at(MON, "10:00"))
    assert active is not None and active.id == "school"


def test_quiet_until_takes_the_latest_of_overlapping_windows():
    """A nap inside school hours must not end quiet time early."""
    quiet = QuietHours(
        FakeConfig(
            [
                window(id="nap", start="13:00", end="14:30"),
                window(id="school", start="09:00", end="15:15"),
            ]
        )
    )
    until = quiet.quiet_until(at(MON, "13:30"))
    assert until is not None
    assert until.hour == 15 and until.minute == 15


def test_quiet_until_handles_the_wrapping_case():
    quiet = QuietHours(FakeConfig([window(id="bed", start="19:00", end="07:00")]))

    # Late evening: the window ends tomorrow morning.
    until = quiet.quiet_until(at(MON, "22:00"))
    assert until is not None and until.day == 28 and until.hour == 7

    # Early morning: it ends later the same day.
    until = quiet.quiet_until(at(MON, "02:00"))
    assert until is not None and until.day == 27 and until.hour == 7


def test_malformed_window_is_skipped_not_fatal():
    quiet = QuietHours(FakeConfig([{"id": "bad", "start": "nope", "enabled": True}]))
    assert quiet.windows() == []
    assert not quiet.is_quiet(at(MON, "12:00"))


def test_window_from_dict_defaults_to_every_day():
    parsed = QuietWindow.from_dict({"id": "x", "start": "01:00", "end": "02:00"})
    assert parsed.days == frozenset(range(7))
