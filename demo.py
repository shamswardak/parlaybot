#!/usr/bin/env python3
"""Offline demo: build tickets from synthetic game logs and print them.

Runs the full trend -> builder -> report pipeline with no network, so you can
see the output format and sanity-check the math before wiring up Discord.
"""

from __future__ import annotations

import math
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parlaybot import discord_out
from parlaybot.builder import BuildConfig, build_slate
from parlaybot.trends import TrendConfig, build_legs_for_player
from tests.fixtures import MARKETS, make_players, make_slate


def main() -> None:
    slate = make_slate(n_games=13)
    trend_cfg = TrendConfig()

    legs = []
    for player, matchup in make_players(slate, per_team=8):
        legs.extend(
            build_legs_for_player(
                player,
                markets=MARKETS,
                game_id=matchup.game_id,
                game_label=matchup.label,
                is_home=player.team == matchup.home_team,
                short_rest=False,
                cfg=trend_cfg,
                hold=0.06,
                price_band=(-1200, -400),
            )
        )

    print(f"{len(legs)} candidate legs over {len({l.game_id for l in legs})} games\n")

    parlays = build_slate(legs, BuildConfig(), date.today())
    print(discord_out.to_console(parlays, date.today(), []))

    print("\n\n=== Monte Carlo check (model probabilities assumed true) ===")
    rng = random.Random(42)
    for p in parlays:
        trials = 200_000
        wins = 0
        for _ in range(trials):
            if all(rng.random() < leg.model_prob for leg in p.legs):
                wins += 1
        sim = wins / trials
        print(
            f"{p.name:>10}: simulated {sim*100:6.3f}%  |  "
            f"analytic {p.model_probability*100:6.3f}%  |  "
            f"break-even {p.implied_probability*100:6.3f}%  |  "
            f"long-run return {sim * p.total_decimal:.3f}x"
        )

    print("\n=== What one ticket a day looks like over a season ===")
    p = parlays[0]
    per_day = p.model_probability
    for days in (30, 90, 180):
        no_win = (1 - per_day) ** days
        print(
            f"{days:3d} tickets: P(at least one hits) = {(1-no_win)*100:5.1f}%   "
            f"expected hits = {per_day*days:.2f}"
        )


if __name__ == "__main__":
    main()
