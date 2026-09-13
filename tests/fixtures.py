"""Synthetic slate generator so the pipeline can be exercised without network."""

from __future__ import annotations

import random
from datetime import date, timedelta

from parlaybot.models import GameLogEntry, Matchup, PlayerSeason

TEAMS = [
    "NYY", "BOS", "LAD", "SDP", "ATL", "PHI", "HOU", "SEA",
    "CHC", "MIL", "BAL", "TBR", "TEX", "ARI", "NYM", "CLE",
]

MARKETS = {
    "hits": {"label": "Hits", "step": 1},
    "total_bases": {"label": "Total Bases", "step": 1},
    "hrr": {"label": "Hits+Runs+RBI", "step": 1},
}


def make_slate(n_games: int = 8, seed: int = 7) -> list[Matchup]:
    rng = random.Random(seed)
    teams = TEAMS[:]
    # Synthesise extra clubs when a test wants more games than the real list
    # can pair up, so fixtures never silently shrink the slate.
    i = 0
    while len(teams) < 2 * n_games:
        teams.append(f"T{i:02d}")
        i += 1
    rng.shuffle(teams)
    games = []
    for i in range(n_games):
        away, home = teams[2 * i], teams[2 * i + 1]
        games.append(
            Matchup(game_id=f"MLB-{9000+i}", sport="MLB",
                    home_team=home, away_team=away)
        )
    return games


def make_player(name: str, team: str, skill: float, seed: int,
                n_games: int = 26) -> PlayerSeason:
    """`skill` is a rough per-game production level; higher means hotter."""
    rng = random.Random(seed)
    today = date.today()
    logs = []
    for i in range(n_games):
        hits = min(4, max(0, int(rng.gauss(skill, 0.75))))
        extra = 1 if rng.random() < 0.22 else 0
        tb = hits + extra
        runs = 1 if rng.random() < 0.30 else 0
        rbi = 1 if rng.random() < 0.32 else 0
        logs.append(
            GameLogEntry(
                game_date=today - timedelta(days=i + 1),
                opponent="OPP",
                home=i % 2 == 0,
                stats={"hits": hits, "total_bases": tb, "hrr": hits + runs + rbi},
                minutes=float(rng.randint(3, 5)),
            )
        )
    return PlayerSeason(player_id=name, name=name, team=team, sport="MLB", logs=logs)


def make_players(matchups: list[Matchup], per_team: int = 7, seed: int = 11
                 ) -> list[tuple[PlayerSeason, Matchup]]:
    rng = random.Random(seed)
    out = []
    n = 0
    for m in matchups:
        for team in (m.home_team, m.away_team):
            for j in range(per_team):
                n += 1
                skill = rng.uniform(0.9, 1.9)
                p = make_player(f"{team} Batter {j+1}", team, skill, seed=seed + n)
                out.append((p, m))
    return out
