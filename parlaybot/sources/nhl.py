"""NHL via api-web.nhle.com -- public, no key."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from ..models import GameLogEntry, Matchup, PlayerSeason
from .base import SportSource

log = logging.getLogger(__name__)

BASE = "https://api-web.nhle.com/v1"

SKATER_MARKETS = {
    "shots": {"label": "Shots on Goal", "step": 1},
    "points": {"label": "Points", "step": 1},
}

GOALIE_MARKETS = {
    "saves": {"label": "Saves", "step": 1},
}


def _season_id(on: date) -> str:
    """NHL seasons run Oct-Jun and are keyed as e.g. 20252026."""
    start = on.year if on.month >= 8 else on.year - 1
    return f"{start}{start + 1}"


def _toi_minutes(toi: str | None) -> float:
    if not toi or ":" not in str(toi):
        return 0.0
    mm, _, ss = str(toi).partition(":")
    try:
        return float(mm) + float(ss) / 60.0
    except ValueError:
        return 0.0


class NHLSource(SportSource):
    sport = "NHL"
    markets = {**SKATER_MARKETS, **GOALIE_MARKETS}

    def __init__(self, client, skaters_per_team: int = 9,
                 lead_minutes: int = 20) -> None:
        super().__init__(client, lead_minutes)
        self.skaters_per_team = skaters_per_team

    def slate(self, on: date) -> list[Matchup]:
        data = self.client.get_json(
            f"{BASE}/schedule/{on.isoformat()}", cache_ttl=1800,
            ttl_tag=f"nhl-sched-{on}"
        )
        out: list[Matchup] = []
        skipped = 0
        for week in (data or {}).get("gameWeek", []):
            if week.get("date") != on.isoformat():
                continue
            for g in week.get("games", []):
                # gameState: FUT (future), PRE (pregame), LIVE, CRIT, OFF, FINAL.
                if not self.is_bettable(g.get("startTimeUTC"),
                                        g.get("gameState", "")):
                    skipped += 1
                    continue
                out.append(
                    Matchup(
                        game_id=f"NHL-{g.get('id')}",
                        sport="NHL",
                        home_team=(g.get("homeTeam") or {}).get("abbrev", ""),
                        away_team=(g.get("awayTeam") or {}).get("abbrev", ""),
                        start_time=g.get("startTimeUTC", ""),
                    )
                )
        log.info("NHL slate: %d bettable games (%d already started)",
                 len(out), skipped)
        return out

    def _roster(self, team: str) -> list[dict]:
        data = self.client.get_json(
            f"{BASE}/roster/{team}/current", cache_ttl=86400, ttl_tag=f"nhl-ros-{team}"
        )
        if not data:
            return []
        rows: list[dict] = []
        for group, is_goalie in (("forwards", False), ("defensemen", False),
                                 ("goalies", True)):
            for p in data.get(group, []):
                first = (p.get("firstName") or {}).get("default", "")
                last = (p.get("lastName") or {}).get("default", "")
                rows.append({
                    "id": p.get("id"),
                    "name": f"{first} {last}".strip(),
                    "goalie": is_goalie,
                })
        return rows

    def players(self, matchups: list[Matchup]) -> list[tuple[PlayerSeason, Matchup]]:
        today = date.today()
        season = _season_id(today)
        out: list[tuple[PlayerSeason, Matchup]] = []

        for m in matchups:
            for team in (m.home_team, m.away_team):
                roster = self._roster(team)
                skaters = [r for r in roster if not r["goalie"]]
                goalies = [r for r in roster if r["goalie"]]
                seasons: list[PlayerSeason] = []

                for r in skaters:
                    ps = self._game_log(r, team, season)
                    if ps:
                        seasons.append(ps)

                # Rank skaters by ice time so we price the players with real roles.
                seasons.sort(
                    key=lambda p: -(sum(g.minutes or 0 for g in p.logs[:10])
                                    / max(1, len(p.logs[:10])))
                )
                for ps in seasons[: self.skaters_per_team]:
                    out.append((ps, m))

                for r in goalies[:1]:
                    ps = self._game_log(r, team, season, goalie=True)
                    if ps:
                        out.append((ps, m))

        log.info("NHL players: %d", len(out))
        return out

    def _game_log(
        self, row: dict, team: str, season: str, goalie: bool = False
    ) -> PlayerSeason | None:
        pid = row.get("id")
        if not pid:
            return None
        data = self.client.get_json(
            f"{BASE}/player/{pid}/game-log/{season}/2",
            cache_ttl=21600,
            ttl_tag=f"nhl-log-{pid}-{season}",
        )
        entries = (data or {}).get("gameLog", [])
        if not entries:
            return None

        logs: list[GameLogEntry] = []
        for e in entries:
            try:
                gd = datetime.strptime(e["gameDate"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if goalie:
                shots_against = float(e.get("shotsAgainst", 0) or 0)
                goals_against = float(e.get("goalsAgainst", 0) or 0)
                saves = e.get("saves")
                saves = (float(saves) if saves is not None
                         else max(0.0, shots_against - goals_against))
                stats = {"saves": saves}
                playing_time = _toi_minutes(e.get("toi")) or shots_against
            else:
                stats = {
                    "shots": float(e.get("shots", 0) or 0),
                    "points": float(e.get("points", 0) or 0),
                }
                playing_time = _toi_minutes(e.get("toi"))
            logs.append(
                GameLogEntry(
                    game_date=gd,
                    opponent=e.get("opponentAbbrev", ""),
                    home=e.get("homeRoadFlag") == "H",
                    stats=stats,
                    minutes=playing_time or None,
                )
            )

        logs.sort(key=lambda g: g.game_date, reverse=True)
        return PlayerSeason(
            player_id=str(pid),
            name=row["name"],
            team=team,
            sport="NHL",
            position="G" if goalie else "",
            logs=logs,
        )

    def markets_for(self, player: PlayerSeason) -> dict[str, dict]:
        return GOALIE_MARKETS if player.position == "G" else SKATER_MARKETS

    def short_rest(self, player: PlayerSeason, on: date) -> bool:
        if not player.logs:
            return False
        return (on - player.logs[0].game_date) <= timedelta(days=1)

    def actual(self, player_id: str, stat_key: str, on: date) -> float | None:
        goalie = stat_key in GOALIE_MARKETS
        player = self._game_log(
            {"id": player_id, "name": ""}, "", _season_id(on), goalie=goalie
        )
        return self._from_logs(player, stat_key, on)
