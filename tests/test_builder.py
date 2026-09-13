"""Ticket assembly against per-ticket criteria."""

from datetime import date

import pytest

from parlaybot.builder import (
    DEFAULT_SPECS,
    TicketSpec,
    build_slate,
    build_ticket,
    eligible,
)
from parlaybot.odds import american_to_prob
from parlaybot.trends import TrendConfig, build_legs_for_player

from .fixtures import MARKETS, make_players, make_slate

WIDE = (-1400, -150)


def _legs(n_games=10, per_team=8):
    slate = make_slate(n_games)
    cfg = TrendConfig()
    legs = []
    for player, matchup in make_players(slate, per_team=per_team):
        legs.extend(
            build_legs_for_player(
                player, markets=MARKETS, game_id=matchup.game_id,
                game_label=matchup.label,
                is_home=player.team == matchup.home_team, short_rest=False,
                cfg=cfg, hold=0.06, price_band=WIDE,
            )
        )
    return legs, slate


SAFE = TicketSpec(name="Safe", n_legs=20, price_min=-1400, price_max=-800,
                  require_current_season=False)
CORE = TicketSpec(name="Core", n_legs=10, price_min=-700, price_max=-450,
                  sports=["MLB"])
TREND = TicketSpec(name="Trend", n_legs=5, price_min=-400, price_max=-150,
                   sports=["MLB"], min_streak=5, max_legs_per_game=1)


# -- the core promise: every leg meets its ticket's criteria ----------------

def test_safe_ticket_legs_are_all_in_band():
    legs, _ = _legs(13)
    t = build_ticket(legs, SAFE, date.today())
    assert t is not None
    assert not t.notes, "a 13-game slate should fill this without loosening"
    for leg in t.legs:
        assert -1400 <= leg.est_price <= -800


def test_core_ticket_legs_are_all_in_band():
    legs, _ = _legs(13)
    t = build_ticket(legs, CORE, date.today())
    for leg in t.legs:
        assert -700 <= leg.est_price <= -450
        assert leg.sport == "MLB"


def test_trend_ticket_enforces_the_streak():
    legs, _ = _legs(13)
    t = build_ticket(legs, TREND, date.today())
    assert t is not None
    # Unrelaxed it must be 5+; if it had to loosen, the note says so.
    if not t.notes:
        for leg in t.legs:
            assert leg.streak >= 5


def test_leg_counts_are_respected():
    legs, _ = _legs(13)
    for spec in (SAFE, CORE, TREND):
        t = build_ticket(legs, spec, date.today())
        assert t is not None and len(t.legs) <= spec.n_legs


def test_no_duplicate_players_within_a_ticket():
    legs, _ = _legs(13)
    t = build_ticket(legs, SAFE, date.today())
    names = [l.player for l in t.legs]
    assert len(names) == len(set(names))


def test_per_game_cap_is_respected():
    legs, _ = _legs(13)
    t = build_ticket(legs, TREND, date.today())   # cap of 1
    per_game = {}
    for leg in t.legs:
        per_game[leg.game_id] = per_game.get(leg.game_id, 0) + 1
    assert max(per_game.values()) == 1


# -- relaxation -------------------------------------------------------------

def test_band_widens_in_probability_space():
    """Widening by American points would move -900 far less than -200."""
    spec = TicketSpec(name="x", n_legs=5, price_min=-1000, price_max=-800)
    lo0, hi0 = spec.band_at(0)
    lo1, hi1 = spec.band_at(1)
    assert american_to_prob(lo1) > american_to_prob(lo0)   # heavier end heavier
    assert american_to_prob(hi1) < american_to_prob(hi0)   # lighter end lighter


def test_streak_requirement_relaxes_one_step_at_a_time():
    """Eased for the reserved steps, then held while the band takes over."""
    spec = TicketSpec(name="x", n_legs=5, price_min=-400, price_max=-150,
                      min_streak=5, streak_relax_steps=2)
    assert [spec.streak_at(i) for i in range(4)] == [5, 4, 3, 3]


def test_streak_never_relaxes_below_zero():
    spec = TicketSpec(name="x", n_legs=5, price_min=-400, price_max=-150,
                      min_streak=1)
    assert spec.streak_at(9) == 0


def test_a_thin_slate_loosens_and_says_so():
    legs, _ = _legs(2, per_team=4)
    spec = TicketSpec(name="x", n_legs=20, price_min=-1400, price_max=-800,
                      absolute_min_legs=1)
    t = build_ticket(legs, spec, date.today())
    assert t is not None
    assert t.notes, "a ticket that could not be filled as specified must say so"


def test_impossible_ticket_returns_none():
    legs, _ = _legs(1, per_team=1)
    spec = TicketSpec(name="x", n_legs=20, price_min=-1400, price_max=-1390,
                      absolute_min_legs=10, relax_steps=0)
    assert build_ticket(legs, spec, date.today()) is None


# -- filtering --------------------------------------------------------------

def test_sport_filter_excludes_other_sports():
    legs, _ = _legs(6)
    spec = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150,
                      sports=["NFL"])
    assert eligible(legs, spec, WIDE, 0, None) == []


def test_current_season_requirement_filters_prior_season_legs():
    legs, _ = _legs(6)
    for leg in legs:
        leg.prior_season_only = True
    strict = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150)
    lax = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150,
                     require_current_season=False)
    assert eligible(legs, strict, WIDE, 0, None) == []
    assert eligible(legs, lax, WIDE, 0, None) != []


def test_min_season_games_is_applied_per_sport():
    legs, _ = _legs(6)
    for leg in legs:
        leg.current_games = 5
    spec = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150)
    assert eligible(legs, spec, WIDE, 0, {"MLB": 4}) != []
    assert eligible(legs, spec, WIDE, 0, {"MLB": 8}) == []


def test_safe_ticket_ignores_the_season_sample_rule():
    """The price is the safety, so Week 1 lines qualify."""
    legs, _ = _legs(6)
    for leg in legs:
        leg.current_games = 0
        leg.prior_season_only = True
    assert eligible(legs, SAFE, (-1400, -800), 0, {"MLB": 8}) != []


# -- the full slate ---------------------------------------------------------

def test_slate_builds_every_ticket_without_sharing_players():
    legs, _ = _legs(13)
    tickets = build_slate(legs, [SAFE, CORE, TREND], date.today())
    assert len(tickets) >= 2
    seen = set()
    for t in tickets:
        names = {l.player for l in t.legs}
        assert not (names & seen), "tickets must not share a player"
        seen |= names


def test_default_specs_match_the_agreed_shape():
    names = [s.name for s in DEFAULT_SPECS]
    counts = [s.n_legs for s in DEFAULT_SPECS]
    assert counts == [20, 10, 5]
    assert names[0].startswith("Safe")
    assert DEFAULT_SPECS[0].require_current_season is False
    assert DEFAULT_SPECS[2].min_streak == 5
    assert DEFAULT_SPECS[0].n_legs <= 20, "DraftKings caps parlays at 20 legs"


def test_average_leg_price_is_probability_weighted():
    legs, _ = _legs(13)
    t = build_ticket(legs, SAFE, date.today())
    avg = t.average_leg_price
    prices = [l.est_price for l in t.legs]
    assert min(prices) <= avg <= max(prices)


def test_streak_relaxes_before_the_price_band_moves():
    """A trend ticket that widens to -900 has become the safe ticket."""
    spec = TicketSpec(name="x", n_legs=5, price_min=-400, price_max=-150,
                      min_streak=5, streak_relax_steps=2)
    assert spec.band_at(0) == spec.band_at(1) == spec.band_at(2)
    assert spec.streak_at(0) == 5 and spec.streak_at(2) == 3
    # Only once the streak budget is spent does the band give.
    assert spec.band_at(3) != spec.band_at(2)
    assert spec.streak_at(3) == 3


def test_band_moves_immediately_when_no_streak_is_required():
    spec = TicketSpec(name="x", n_legs=20, price_min=-1400, price_max=-800)
    assert spec.band_at(1) != spec.band_at(0)
