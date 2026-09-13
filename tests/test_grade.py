import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from parlaybot.grade import (
    HIT,
    MISS,
    VOID,
    LegResult,
    TicketResult,
    append_graded_legs,
    running_record,
    save_results,
)


def _leg(player="A", threshold=2, price=-600, prob=0.86, stat="hits"):
    return {
        "player": player,
        "player_id": f"id-{player}",
        "sport": "MLB",
        "stat_key": stat,
        "threshold": threshold,
        "market": "Hits",
        "selection": f"{player} {threshold}+ Hits",
        "est_price": price,
        "model_prob": prob,
        "confidence": 0.7,
        "streak": 5,
    }


def _result(statuses, actuals=None):
    actuals = actuals or [None] * len(statuses)
    legs = [
        LegResult(leg=_leg(player=f"P{i}"), status=s, actual=a)
        for i, (s, a) in enumerate(zip(statuses, actuals))
    ]
    return TicketResult(name="T", slate_date=date(2026, 9, 12), legs=legs,
                        total_american=1500, model_probability=0.04)


def test_all_hits_is_a_win():
    assert _result([HIT] * 5, [3, 3, 3, 3, 3]).status == "WIN"


def test_one_miss_loses_the_ticket():
    assert _result([HIT, HIT, MISS, HIT], [3, 3, 0, 3]).status == "LOSS"


def test_void_without_a_miss_is_pending():
    assert _result([HIT, VOID, HIT], [3, None, 3]).status == "PENDING"


def test_a_miss_beats_a_void():
    """A void can't rescue a ticket that already has a loser on it."""
    assert _result([MISS, VOID], [0, None]).status == "LOSS"


def test_single_leg_death_is_reported_with_the_margin():
    r = _result([HIT, MISS, HIT], [3, 1, 3])
    note = r.near_miss_note()
    assert "one leg" in note
    assert "by 1" in note


def test_multi_leg_death_is_counted():
    r = _result([MISS, MISS, HIT], [0, 0, 3])
    assert "Died on 2 legs" == r.near_miss_note()


def test_winner_has_no_death_note():
    assert _result([HIT, HIT], [3, 3]).near_miss_note() is None


def test_voids_shorten_the_settled_price():
    """A book drops void legs and repays at the shortened price."""
    full = _result([HIT, HIT, HIT], [3, 3, 3])
    with_void = _result([HIT, HIT, VOID], [3, 3, None])
    assert with_void.settled_decimal < full.settled_decimal


def test_margin_is_none_without_a_result():
    r = LegResult(leg=_leg(), status=VOID, actual=None)
    assert r.margin is None
    assert "no result" in r.describe()


def test_describe_shows_what_was_needed():
    r = LegResult(leg=_leg(threshold=2), status=MISS, actual=1)
    assert "only got 1" in r.describe()
    assert "needed 2" in r.describe()


def test_ledger_skips_voids_and_deduplicates():
    with tempfile.TemporaryDirectory() as d:
        r = _result([HIT, MISS, VOID], [3, 0, None])
        first = append_graded_legs(d, date(2026, 9, 12), [r])
        assert first == 2, "voids must not enter the calibration ledger"

        again = append_graded_legs(d, date(2026, 9, 12), [r])
        assert again == 0, "re-grading a day must not double-count"

        rows = [json.loads(x) for x in
                (Path(d) / "legs.jsonl").read_text().splitlines()]
        assert {r["hit"] for r in rows} == {True, False}


def test_running_record_counts_across_files():
    with tempfile.TemporaryDirectory() as d:
        save_results(d, date(2026, 9, 11), [_result([HIT, HIT], [3, 3])])
        save_results(d, date(2026, 9, 12), [_result([MISS], [0]),
                                            _result([HIT], [3])])
        rec = running_record(d)
        assert rec["wins"] == 2
        assert rec["losses"] == 1
        assert rec["tickets"] == 3
