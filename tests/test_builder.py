import math
from datetime import date

import pytest

from parlaybot.builder import BuildConfig, build_parlay, build_slate, build_slots
from parlaybot.trends import TrendConfig, build_legs_for_player

from .fixtures import MARKETS, make_players, make_slate


def _legs(n_games=10, per_team=7):
    slate = make_slate(n_games)
    cfg = TrendConfig()
    legs = []
    for player, matchup in make_players(slate, per_team=per_team):
        legs.extend(
            build_legs_for_player(
                player,
                markets=MARKETS,
                game_id=matchup.game_id,
                game_label=matchup.label,
                is_home=player.team == matchup.home_team,
                short_rest=False,
                cfg=cfg,
                hold=0.06,
                price_band=(-1200, -400),
            )
        )
    return legs, slate


def test_fixture_slate_produces_candidates():
    legs, _ = _legs()
    assert len(legs) > 40
    assert all(-1200 <= l.est_price <= -400 for l in legs)
    assert all(0.0 <= l.model_prob <= 1.0 for l in legs)


def test_builder_hits_the_payout_target():
    legs, _ = _legs()
    cfg = BuildConfig(target_american=1500, min_legs=15, max_legs=20)
    parlay = build_parlay(legs, cfg, date.today(), n_legs=18)
    assert parlay is not None
    err = abs(math.log(parlay.total_decimal / cfg.target_decimal))
    assert err <= cfg.tolerance, (
        f"payout {parlay.total_american:.0f} missed target {cfg.target_american}"
    )


def test_leg_count_is_respected():
    legs, _ = _legs()
    cfg = BuildConfig()
    for n in (15, 18, 20):
        p = build_parlay(legs, cfg, date.today(), n_legs=n)
        assert p is not None and len(p.legs) == n


def test_no_duplicate_players_on_a_ticket():
    legs, _ = _legs()
    p = build_parlay(legs, BuildConfig(), date.today(), n_legs=20)
    names = [l.player for l in p.legs]
    assert len(names) == len(set(names))


def test_cross_game_preference_uses_every_game_first():
    """With 10 games and 20 legs, each game should appear exactly twice --
    never three times in one game while another game is unused."""
    legs, slate = _legs(n_games=10)
    p = build_parlay(legs, BuildConfig(max_legs_per_game=2), date.today(), n_legs=20)
    per_game = {}
    for leg in p.legs:
        per_game[leg.game_id] = per_game.get(leg.game_id, 0) + 1
    assert max(per_game.values()) <= 2
    assert len(per_game) == 10


def test_thin_slate_builds_the_best_available_ticket():
    """A six-game night should still produce a ticket, not a refusal."""
    legs, _ = _legs(n_games=6)
    cfg = BuildConfig(min_legs=15, max_legs=20, max_legs_per_game=2)
    p = build_parlay(legs, cfg, date.today(), n_legs=20)
    assert p is not None
    # 6 games x 2 legs per game is the ceiling.
    assert len(p.legs) <= 12
    assert len(p.legs) >= cfg.absolute_min_legs
    assert p.notes, "a short ticket must say that it is short"
    assert "Short slate" in p.notes[0]


def test_thin_slate_still_pushes_toward_the_payout_target():
    """Fewer legs means each must be longer; the tuner should still try."""
    legs, _ = _legs(n_games=6)
    p = build_parlay(legs, BuildConfig(), date.today(), n_legs=20)
    assert p.total_decimal > 5.0


def test_below_absolute_minimum_returns_none():
    legs, _ = _legs(n_games=1, per_team=1)
    cfg = BuildConfig(absolute_min_legs=8, max_legs_per_game=2)
    assert build_parlay(legs, cfg, date.today(), n_legs=20) is None


def test_full_slate_still_honours_the_requested_leg_count():
    """The fallback must not make the builder lazy on a healthy slate."""
    legs, _ = _legs(n_games=13)
    p = build_parlay(legs, BuildConfig(), date.today(), n_legs=20)
    assert len(p.legs) == 20
    assert not p.notes


def test_slate_tickets_do_not_share_players():
    legs, _ = _legs(n_games=12)
    parlays = build_slate(legs, BuildConfig(), date.today())
    assert len(parlays) >= 2
    seen = set()
    for p in parlays:
        names = {l.player for l in p.legs}
        assert not (names & seen), "tickets share a player"
        seen |= names


def test_sgp_legs_are_flagged():
    legs, _ = _legs(n_games=10)
    p = build_parlay(legs, BuildConfig(max_legs_per_game=2), date.today(), n_legs=20)
    # 20 legs over 10 games means every game is doubled up.
    assert len(p.sgp_legs) == 20
    assert p.n_games == 10


def test_slots_group_ladders_per_player_market():
    legs, _ = _legs(n_games=4, per_team=5)
    slots = build_slots(legs)
    for slot in slots:
        thresholds = [r.threshold for r in slot.rungs]
        assert thresholds == sorted(thresholds)
        assert len({r.player for r in slot.rungs}) == 1


def test_parlay_math_is_internally_consistent():
    legs, _ = _legs()
    p = build_parlay(legs, BuildConfig(), date.today(), n_legs=18)
    assert p.total_decimal == pytest.approx(
        math.prod(l.decimal for l in p.legs), rel=1e-9
    )
    assert p.model_probability == pytest.approx(
        math.prod(l.model_prob for l in p.legs), rel=1e-9
    )
    assert p.expected_value == pytest.approx(
        p.model_probability * p.total_decimal, rel=1e-9
    )
    assert p.implied_probability == pytest.approx(1.0 / p.total_decimal, rel=1e-9)


def test_single_game_slate_still_builds_a_ticket():
    """One game left at 9pm should produce a playable SGP, not nothing."""
    legs, _ = _legs(n_games=1, per_team=8)
    p = build_parlay(legs, BuildConfig(), date.today(), n_legs=20)
    assert p is not None
    assert len(p.legs) >= 4
    assert p.n_games == 1
    assert any("one game" in n for n in p.notes), "must warn it's a same-game parlay"


def test_single_game_ticket_respects_the_ceiling():
    legs, _ = _legs(n_games=1, per_team=8)
    cfg = BuildConfig(thin_slate_max_per_game=6)
    p = build_parlay(legs, cfg, date.today(), n_legs=20)
    assert len(p.legs) <= 6


def test_strict_cap_still_refuses_when_configured():
    """Setting the ceiling to the normal cap restores the old behaviour."""
    legs, _ = _legs(n_games=1, per_team=8)
    cfg = BuildConfig(max_legs_per_game=2, thin_slate_max_per_game=2)
    assert build_parlay(legs, cfg, date.today(), n_legs=20) is None


def test_healthy_slate_does_not_stretch_the_cap():
    """The stretch must only trigger when the slate can't fill the ticket."""
    legs, _ = _legs(n_games=13)
    p = build_parlay(legs, BuildConfig(), date.today(), n_legs=20)
    per_game = {}
    for leg in p.legs:
        per_game[leg.game_id] = per_game.get(leg.game_id, 0) + 1
    assert max(per_game.values()) <= 2
    assert not any("one game" in n for n in p.notes)
