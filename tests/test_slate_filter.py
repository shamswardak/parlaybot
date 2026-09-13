"""The rule that keeps in-progress games off the ticket."""

from datetime import datetime, timedelta, timezone

import pytest

from parlaybot.sources.base import SportSource, parse_utc

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


class _Source(SportSource):
    sport = "TEST"

    def slate(self, on):
        return []

    def players(self, matchups):
        return []


def src(lead=20):
    return _Source(client=None, lead_minutes=lead)


def iso(offset_minutes: int) -> str:
    return (NOW + timedelta(minutes=offset_minutes)).isoformat().replace(
        "+00:00", "Z"
    )


def test_future_game_is_bettable():
    assert src().is_bettable(iso(180), "Scheduled", now=NOW)


def test_game_already_started_is_not():
    assert not src().is_bettable(iso(-30), "Scheduled", now=NOW)


def test_game_starting_inside_the_lead_time_is_not():
    """No point surfacing a game that starts in five minutes."""
    assert not src().is_bettable(iso(5), "Scheduled", now=NOW)
    assert src().is_bettable(iso(25), "Scheduled", now=NOW)


def test_lead_time_is_configurable():
    assert src(lead=120).is_bettable(iso(180), now=NOW)
    assert not src(lead=120).is_bettable(iso(60), now=NOW)


def test_live_status_is_rejected_even_with_a_future_timestamp():
    """Status wins over the clock — a bad timestamp must not let a live game in."""
    for state in ("Live", "In Progress", "LIVE", "CRIT"):
        assert not src().is_bettable(iso(180), state, now=NOW)


def test_finished_and_postponed_are_rejected():
    for state in ("Final", "Game Over", "Postponed", "Suspended", "Cancelled",
                  "OFF", "FINAL"):
        assert not src().is_bettable(iso(180), state, now=NOW)


def test_pregame_states_are_accepted():
    for state in ("Scheduled", "Pre-Game", "Warmup", "FUT", "PRE", ""):
        assert src().is_bettable(iso(180), state, now=NOW)


def test_missing_start_time_falls_back_to_status():
    """A feed without a timestamp shouldn't lose us the whole slate."""
    assert src().is_bettable(None, "Scheduled", now=NOW)
    assert not src().is_bettable(None, "Final", now=NOW)


def test_unparseable_timestamp_does_not_crash():
    assert src().is_bettable("not a date", "Scheduled", now=NOW)


def test_parse_utc_handles_the_formats_these_apis_use():
    assert parse_utc("2026-09-13T23:05:00Z").hour == 23
    assert parse_utc("2026-09-13T23:05:00+00:00").hour == 23
    # Naive timestamps are assumed UTC rather than rejected.
    assert parse_utc("2026-09-13T23:05:00").tzinfo is timezone.utc
    assert parse_utc("") is None
    assert parse_utc(None) is None


def test_datetime_objects_are_accepted_directly():
    assert src().is_bettable(NOW + timedelta(hours=2), now=NOW)
    assert not src().is_bettable(NOW - timedelta(hours=2), now=NOW)


def test_sources_report_what_they_dropped():
    """main.py distinguishes 'nothing scheduled' from 'all started' using this."""
    s = src()
    assert s.last_skipped == 0
    s.last_skipped = 15
    assert getattr(s, "last_skipped", 0) == 15
