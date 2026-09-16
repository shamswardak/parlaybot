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
    assert eligible(legs, spec, 0, 0, None) == []


def test_current_season_requirement_filters_prior_season_legs():
    legs, _ = _legs(6)
    for leg in legs:
        leg.prior_season_only = True
    strict = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150)
    lax = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150,
                     require_current_season=False)
    assert eligible(legs, strict, 0, 0, None) == []
    assert eligible(legs, lax, 0, 0, None) != []


def test_min_season_games_is_applied_per_sport():
    legs, _ = _legs(6)
    for leg in legs:
        leg.current_games = 5
    spec = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150)
    assert eligible(legs, spec, 0, 0, {"MLB": 4}) != []
    assert eligible(legs, spec, 0, 0, {"MLB": 8}) == []


def test_safe_ticket_ignores_the_season_sample_rule():
    """The price is the safety, so Week 1 lines qualify."""
    legs, _ = _legs(6)
    for leg in legs:
        leg.current_games = 0
        leg.prior_season_only = True
    assert eligible(legs, SAFE, 0, 0, {"MLB": 8}) != []


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
    """One ticket a day. Safe 20 and Trend 5 were retired on the record:
    both sat below their break-even, and Safe 20 could not clear it at any
    per-leg accuracy because twenty legs compounds the error."""
    assert len(DEFAULT_SPECS) == 1, "the schedule builds exactly one ticket"
    spec = DEFAULT_SPECS[0]
    assert spec.name == "Daily Ticket"
    assert spec.n_legs == 10
    assert spec.n_legs <= 20, "DraftKings caps parlays at 20 legs"
    assert spec.require_current_season is True
    assert spec.relax_steps == 2, (
        "five passes stretched the band to -314/-1438, which is no longer the "
        "ticket these criteria describe"
    )


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


def test_per_market_bands_override_the_ticket_band():
    """Pitcher props get their own heavier band; hitters keep the default."""
    spec = TicketSpec(name="x", n_legs=20, price_min=-700, price_max=-550,
                      market_bands={"Strikeouts": [-1600, -800]})
    assert spec.band_for("Strikeouts", 0) != spec.band_for("Hits", 0)
    assert min(spec.band_for("Strikeouts", 0)) < min(spec.band_for("Hits", 0))
    # An unlisted market falls back to the ticket band.
    assert spec.band_for("Hits", 0) == spec.band_for(None, 0)


def test_market_band_admits_a_leg_the_ticket_band_would_reject():
    legs, _ = _legs(6)
    for leg in legs:
        leg.market, leg.est_price = "Strikeouts", -1200.0
    narrow = TicketSpec(name="x", n_legs=5, price_min=-700, price_max=-550)
    wide = TicketSpec(name="x", n_legs=5, price_min=-700, price_max=-550,
                      market_bands={"Strikeouts": [-1600, -800]})
    assert eligible(legs, narrow, 0, 0, None) == []
    assert eligible(legs, wide, 0, 0, None) != []


def test_preferred_markets_are_filled_first():
    from parlaybot.builder import best_per_player
    legs, _ = _legs(6)
    for i, leg in enumerate(legs):
        leg.market = "Strikeouts" if i % 2 else "Hits"
    ranked = best_per_player(legs, (-1400, -150), "safe", ["Strikeouts"])
    assert ranked[0].market == "Strikeouts", "preferred markets go first"


def test_stale_trend_legs_are_kept_off_trend_tickets():
    """A streak that may already be broken can't carry a trend ticket."""
    legs, _ = _legs(6)
    for leg in legs:
        leg.stale_trend = True
    trend = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150)
    safe = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150,
                      allow_stale_trend=True)
    assert eligible(legs, trend, 0, 0, None) == []
    assert eligible(legs, safe, 0, 0, None) != [], (
        "the safe ticket leans on price, not on the streak, so it may accept "
        "a leg whose last game is unresolved"
    )


def test_the_daily_ticket_refuses_stale_trends():
    """A leg whose window is missing an unfinished game may already be broken.
    Only a ticket carried by price can take that; the daily ticket is carried
    by form, so it must not."""
    assert DEFAULT_SPECS[0].allow_stale_trend is False


def test_tickets_are_no_longer_pinned_to_one_sport():
    for spec in DEFAULT_SPECS:
        assert spec.sports is None, f"{spec.name} should not restrict sports"


def test_mlb_is_preferred_but_not_required():
    """MLB fills first; other sports fill behind it rather than being excluded."""
    from parlaybot.builder import best_per_player
    legs, _ = _legs(6)
    for i, leg in enumerate(legs):
        leg.sport = "NFL" if i % 2 else "MLB"
    ranked = best_per_player(legs, WIDE, "trend", None, ["MLB"])
    assert ranked[0].sport == "MLB"
    assert any(l.sport == "NFL" for l in ranked), "NFL must still be available"


def test_waiving_the_sample_rule_lets_a_thin_sport_through():
    """Choosing NFL on its own should produce legs even in Week 1."""
    legs, _ = _legs(6)
    for leg in legs:
        leg.sport, leg.current_games, leg.prior_season_only = "NFL", 0, True
    spec = TicketSpec(name="x", n_legs=5, price_min=-1400, price_max=-150,
                      require_current_season=True)
    assert eligible(legs, spec, 0, 0, {"NFL": 4}) == []
    assert eligible(legs, spec, 0, 0, {"NFL": 4}, waive_sample=True) != []


def test_waiver_does_not_bypass_price_or_streak_criteria():
    """It relaxes the sample rule only — the ticket's own shape still holds."""
    legs, _ = _legs(6)
    for leg in legs:
        leg.sport, leg.current_games, leg.prior_season_only = "NFL", 0, True
        leg.est_price = -2000.0
    spec = TicketSpec(name="x", n_legs=5, price_min=-700, price_max=-450)
    assert eligible(legs, spec, 0, 0, None, waive_sample=True) == []
