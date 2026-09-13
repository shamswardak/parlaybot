"""NFL via nflverse-data release CSVs -- no key, hosted on GitHub releases.

nflverse renames release assets between seasons, so every download walks a list
of candidate URLs and takes the first that responds.
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta

import pandas as pd

from ..models import GameLogEntry, Matchup, PlayerSeason
from .base import SportSource

log = logging.getLogger(__name__)

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"

MARKETS = {
    "passing_yards": {"label": "Passing Yards", "step": 25},
    "completions": {"label": "Completions", "step": 1},
    "rushing_yards": {"label": "Rushing Yards", "step": 5},
    "receiving_yards": {"label": "Receiving Yards", "step": 5},
    "receptions": {"label": "Receptions", "step": 1},
}

# Upstream has used several names for the same columns across versions.
COLUMN_ALIASES = {
    "player_display_name": ["player_display_name", "player_name", "display_name"],
    "team": ["team", "recent_team", "team_abbr"],
    "player_id": ["player_id", "gsis_id"],
    "position": ["position", "position_group"],
}


def _pick(df: pd.DataFrame, logical: str) -> str | None:
    for name in COLUMN_ALIASES[logical]:
        if name in df.columns:
            return name
    return None


class NFLSource(SportSource):
    sport = "NFL"
    markets = MARKETS

    def __init__(self, client, lookback_seasons: int = 2) -> None:
        super().__init__(client)
        self.lookback_seasons = lookback_seasons
        self._weekly: pd.DataFrame | None = None
        self._schedule: pd.DataFrame | None = None

    # -- downloads ---------------------------------------------------------

    def _load_schedule(self) -> pd.DataFrame:
        if self._schedule is not None:
            return self._schedule
        body = self.client.first_ok(
            [
                f"{RELEASES}/schedules/games.csv",
                "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
                "http://www.habitatring.com/games.csv",
            ],
            cache_ttl=21600,
        )
        self._schedule = pd.read_csv(io.StringIO(body)) if body else pd.DataFrame()
        return self._schedule

    def _load_weekly(self, season: int) -> pd.DataFrame:
        body = self.client.first_ok(
            [
                f"{RELEASES}/stats_player/stats_player_week_{season}.csv",
                f"{RELEASES}/player_stats/player_stats_{season}.csv",
                f"{RELEASES}/stats_player/stats_player_week_{season}.csv.gz",
            ],
            cache_ttl=21600,
        )
        if not body:
            log.warning("NFL weekly stats unavailable for %s", season)
            return pd.DataFrame()
        return pd.read_csv(io.StringIO(body), low_memory=False)

    def _weekly_frame(self, season: int) -> pd.DataFrame:
        if self._weekly is not None:
            return self._weekly
        frames = [
            self._load_weekly(s)
            for s in range(season - self.lookback_seasons + 1, season + 1)
        ]
        frames = [f for f in frames if not f.empty]
        self._weekly = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return self._weekly

    # -- slate -------------------------------------------------------------

    def slate(self, on: date) -> list[Matchup]:
        sched = self._load_schedule()
        if sched.empty or "gameday" not in sched.columns:
            return []
        day = sched[sched["gameday"].astype(str) == on.isoformat()]
        out: list[Matchup] = []
        for _, row in day.iterrows():
            out.append(
                Matchup(
                    game_id=f"NFL-{row.get('game_id', f'{row.home_team}{row.away_team}')}",
                    sport="NFL",
                    home_team=str(row["home_team"]),
                    away_team=str(row["away_team"]),
                    start_time=str(row.get("gametime", "")),
                )
            )
        log.info("NFL slate: %d games", len(out))
        return out

    # -- players -----------------------------------------------------------

    def players(self, matchups: list[Matchup]) -> list[tuple[PlayerSeason, Matchup]]:
        season = datetime.now().year
        df = self._weekly_frame(season)
        if df.empty:
            return []

        name_col = _pick(df, "player_display_name")
        team_col = _pick(df, "team")
        id_col = _pick(df, "player_id")
        pos_col = _pick(df, "position")
        if not (name_col and team_col and id_col):
            log.warning("NFL weekly stats missing expected columns: %s",
                        list(df.columns)[:25])
            return []

        if "season_type" in df.columns:
            df = df[df["season_type"].isin(["REG", "POST"])]
        df = df.sort_values(["season", "week"], ascending=[False, False])

        team_to_game = {}
        for m in matchups:
            team_to_game[m.home_team] = m
            team_to_game[m.away_team] = m

        out: list[tuple[PlayerSeason, Matchup]] = []
        active = df[df[team_col].isin(team_to_game.keys())]

        for pid, group in active.groupby(id_col):
            matchup = team_to_game.get(str(group.iloc[0][team_col]))
            if matchup is None:
                continue
            logs = self._logs_from_rows(group, matchup)
            if len(logs) < 6:
                continue
            out.append((
                PlayerSeason(
                    player_id=str(pid),
                    name=str(group.iloc[0][name_col]),
                    team=str(group.iloc[0][team_col]),
                    sport="NFL",
                    position=str(group.iloc[0][pos_col]) if pos_col else "",
                    logs=logs,
                ),
                matchup,
            ))

        log.info("NFL players: %d", len(out))
        return out

    def _logs_from_rows(self, group: pd.DataFrame, matchup: Matchup
                        ) -> list[GameLogEntry]:
        logs: list[GameLogEntry] = []
        for _, row in group.iterrows():
            stats = {}
            for key in MARKETS:
                if key in row and pd.notna(row[key]):
                    stats[key] = float(row[key])
            if not stats:
                continue
            # Usage proxy: total touches/attempts, to catch role volatility.
            usage = sum(
                float(row[c]) for c in ("carries", "targets", "attempts")
                if c in row and pd.notna(row[c])
            )
            # Synthetic but correctly *ordered* date: season start + week offset.
            week = int(row["week"]) if pd.notna(row.get("week")) else 1
            gd = date(int(row["season"]), 9, 1) + timedelta(weeks=week - 1)
            logs.append(
                GameLogEntry(
                    game_date=gd,
                    opponent=str(row.get("opponent_team", "")),
                    home=False,
                    stats=stats,
                    minutes=usage or None,
                )
            )
        return logs

    def markets_for(self, player: PlayerSeason) -> dict[str, dict]:
        pos = player.position.upper()
        if pos == "QB":
            return {k: v for k, v in MARKETS.items()
                    if k in ("passing_yards", "completions", "rushing_yards")}
        if pos == "RB":
            return {k: v for k, v in MARKETS.items()
                    if k in ("rushing_yards", "receptions", "receiving_yards")}
        return {k: v for k, v in MARKETS.items()
                if k in ("receiving_yards", "receptions")}

    def short_rest(self, player: PlayerSeason, on: date) -> bool:
        return False  # weekly sport; Thursday games are handled by the schedule

    def actual(self, player_id: str, stat_key: str, on: date) -> float | None:
        """Look up the weekly stat row for the week containing `on`.

        nflverse publishes weekly stats a day or two after the games, so a
        Monday grading of Sunday's slate can legitimately find nothing yet --
        that returns None and the leg stays ungraded rather than scoring wrong.
        """
        sched = self._load_schedule()
        if sched.empty or "gameday" not in sched.columns:
            return None
        row = sched[sched["gameday"].astype(str) == on.isoformat()]
        if row.empty:
            return None
        season = int(row.iloc[0]["season"])
        week = int(row.iloc[0]["week"])

        df = self._weekly_frame(season)
        if df.empty:
            return None
        id_col = _pick(df, "player_id")
        if not id_col or "week" not in df.columns:
            return None

        match = df[
            (df[id_col].astype(str) == str(player_id))
            & (df["season"] == season)
            & (df["week"] == week)
        ]
        if match.empty:
            return None
        value = match.iloc[0].get(stat_key)
        return float(value) if pd.notna(value) else None
