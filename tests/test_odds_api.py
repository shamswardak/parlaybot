"""Odds feed parsing and — more importantly — credit protection."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from parlaybot.odds_api import (
    MARKET_MAP,
    SPORT_KEYS,
    Budget,
    OddsClient,
    best_offers,
    parse_event,
    threshold_from_point,
)

SAMPLE = {
    "id": "evt1",
    "home_team": "Chicago Cubs",
    "away_team": "Pittsburgh Pirates",
    "commence_time": "2026-09-13T18:05:00Z",
    "bookmakers": [
        {"key": "draftkings", "markets": [
            {"key": "pitcher_strikeouts_alternate", "outcomes": [
                {"name": "Over", "description": "Matthew Boyd", "point": 3.5,
                 "price": -900, "link": "https://dk/slip/1"},
                {"name": "Over", "description": "Matthew Boyd", "point": 5.5,
                 "price": -250},
                {"name": "Under", "description": "Matthew Boyd", "point": 5.5,
                 "price": 190},
            ]},
        ]},
        {"key": "fanduel", "markets": [
            {"key": "pitcher_strikeouts_alternate", "outcomes": [
                {"name": "Over", "description": "Matthew Boyd", "point": 3.5,
                 "price": -750},
            ]},
        ]},
    ],
}


# -- parsing ----------------------------------------------------------------

def test_milestone_threshold_is_recovered_from_the_point():
    """'5+ strikeouts' is posted as Over 4.5."""
    assert threshold_from_point(4.5) == 5
    assert threshold_from_point(3.5) == 4
    assert threshold_from_point(49.5) == 50


def test_parse_keeps_only_the_over_side():
    offers = parse_event("MLB", SAMPLE)
    assert offers, "expected offers"
    assert all(o.threshold > 0 for o in offers)
    # The Under outcome must not become a leg.
    assert len(offers) == 3


def test_parse_maps_market_keys_to_our_stat_keys():
    for offer in parse_event("MLB", SAMPLE):
        assert offer.stat_key == "strikeouts"
        assert offer.player == "Matthew Boyd"
        assert offer.game_label == "Pittsburgh Pirates @ Chicago Cubs"


def test_parse_carries_the_betslip_link_when_present():
    offers = parse_event("MLB", SAMPLE)
    assert any(o.link for o in offers), "deep links should survive parsing"


def test_unmapped_markets_are_ignored():
    payload = {**SAMPLE, "bookmakers": [
        {"key": "draftkings", "markets": [
            {"key": "some_market_we_do_not_model", "outcomes": [
                {"name": "Over", "description": "X", "point": 1.5, "price": -200}]}]}]}
    assert parse_event("MLB", payload) == []


def test_best_offers_shops_the_longer_price():
    """DK -900 vs FD -750 on the same line: take FanDuel."""
    best = best_offers(parse_event("MLB", SAMPLE))
    boyd_4 = [o for o in best if o.threshold == 4][0]
    assert boyd_4.price == -750
    assert boyd_4.book == "fanduel"


def test_market_map_covers_every_sport_key():
    for sport in SPORT_KEYS:
        assert sport in MARKET_MAP and MARKET_MAP[sport]


# -- budget -----------------------------------------------------------------

def test_budget_rolls_over_at_the_month_boundary():
    b = Budget(limit=450, used=400, month="2026-08")
    b.rollover(datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert b.used == 0 and b.month == "2026-09"


def test_budget_does_not_reset_within_a_month():
    b = Budget(limit=450, used=400, month="2026-09")
    b.rollover(datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert b.used == 400


def test_budget_refuses_to_overspend():
    b = Budget(limit=450, used=445, month="2026-09")
    assert b.can_spend(5)
    assert not b.can_spend(6)


def test_budget_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "budget.json"
        c1 = OddsClient("key", cache_dir=d, state_path=state, monthly_limit=450)
        c1.budget.used = 120
        c1.save_budget()
        c2 = OddsClient("key", cache_dir=d, state_path=state, monthly_limit=450)
        assert c2.budget.used == 120, "spend must persist between runs"


def test_corrupt_budget_state_does_not_crash_the_run():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "budget.json"
        state.write_text("{ not json")
        client = OddsClient("key", cache_dir=d, state_path=state)
        assert client.budget.used == 0


def test_exhausted_budget_skips_the_request_instead_of_failing():
    """Degrade to modelled prices rather than blowing up the run."""
    with tempfile.TemporaryDirectory() as d:
        client = OddsClient("key", cache_dir=d,
                            state_path=Path(d) / "b.json", monthly_limit=0)
        assert client.events("MLB") == []
        assert client.spent_this_run == 0


def test_cache_hit_costs_nothing():
    with tempfile.TemporaryDirectory() as d:
        client = OddsClient("key", cache_dir=d, state_path=Path(d) / "b.json",
                            cache_ttl=3600)
        body, _ = client._cache_paths("events-MLB")
        body.write_text(json.dumps([{"id": "evt1"}]))
        assert client.events("MLB") == [{"id": "evt1"}]
        assert client.spent_this_run == 0, "a warm cache must not spend credits"


def test_unknown_sport_costs_nothing():
    with tempfile.TemporaryDirectory() as d:
        client = OddsClient("key", cache_dir=d, state_path=Path(d) / "b.json")
        assert client.events("CRICKET") == []
        assert client.spent_this_run == 0


def test_event_offers_cost_is_markets_times_regions():
    """The budget maths the whole free tier depends on."""
    with tempfile.TemporaryDirectory() as d:
        client = OddsClient("key", cache_dir=d, state_path=Path(d) / "b.json",
                            monthly_limit=2)
        # 3 markets x 1 region = 3 credits > the limit of 2, so it must refuse.
        out = client.event_offers("MLB", {"id": "e1"},
                                  ["pitcher_strikeouts_alternate",
                                   "batter_hits_alternate",
                                   "batter_total_bases_alternate"])
        assert out == []
        assert client.spent_this_run == 0
