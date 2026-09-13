"""NBA via cdn.nba.com static JSON.

Deliberately avoids stats.nba.com: that host IP-bans cloud providers, which
would break the bot the moment it runs on GitHub Actions. The CDN serves the
same box scores with no such block, so player game logs are reconstructed from
per-game box scores and cached permanently once a game is final.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from ..models import GameLogEntry, Matchup, PlayerSeason
from .base import SportSource

log = logging.getLogger(__name__)

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"
SCHEDULE_FALLBACK = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOXSCORE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"

# The CDN answers 403 to requests that don't look like they came from nba.com.
# Sending the origin headers a browser would send is what gets it to serve.
NBA_HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

MARKETS = {
    "points": {"label": "Points", "step": 1},
    "rebounds": {"label": "Rebounds", "step": 1},
    "assists": {"label": "Assists", "step": 1},
    "pra": {"label": "Pts+Reb+Ast", "step": 1},
    "threes": {"label": "3-Pointers Made", "step": 1},
}

_MIN_RE = re.compile(r"PT(?:(\d+)M)?(?:([\d.]+)S)?")


def _minutes(raw: str | None) -> float:
    if not raw:
        return 0.0
    if ":" in raw:  # "34:12"
        mm, _, ss = raw.partition(":")
        try:
            return float(mm) + float(ss) / 60.0
        except ValueError:
            return 0.0
    m = _MIN_RE.match(raw)
    if not m:
        return 0.0
    mins = float(m.group(1) or 0)
    secs = float(m.group(2) or 0)
    return mins + secs / 60.0


class NBASource(SportSource):
    sport = "NBA"
    markets = MARKETS

    def __init__(self, client, games_back: int = 20) -> None:
        super().__init__(client)
        self.games_back = games_back
        self._schedule: list[dict] | None = None

    # -- schedule ----------------------------------------------------------

    def _load_schedule(self) -> list[dict]:
        if self._schedule is not None:
            return self._schedule
        data = self.client.get_json(
            SCHEDULE_URL, headers=NBA_HEADERS, cache_ttl=21600, ttl_tag="nba-sched"
        )
        if not data:
            data = self.client.get_json(
                SCHEDULE_FALLBACK, headers=NBA_HEADERS, cache_ttl=21600,
                ttl_tag="nba-sched-fb"
            )
        games: list[dict] = []
        for gd in ((data or {}).get("leagueSchedule", {}).get("gameDates", [])):
            raw_date = gd.get("gameDate", "")
            try:
                d = datetime.strptime(raw_date.split(" ")[0], "%m/%d/%Y").date()
            except ValueError:
                continue
            for g in gd.get("games", []):
                games.append({
                    "date": d,
                    "gameId": g.get("gameId"),
                    "home": (g.get("homeTeam") or {}).get("teamTricode", ""),
                    "away": (g.get("awayTeam") or {}).get("teamTricode", ""),
                    "time": g.get("gameDateTimeUTC", ""),
                })
        self._schedule = games
        return games

    def slate(self, on: date) -> list[Matchup]:
        out = [
            Matchup(
                game_id=f"NBA-{g['gameId']}",
                sport="NBA",
                home_team=g["home"],
                away_team=g["away"],
                start_time=g["time"],
            )
            for g in self._load_schedule()
            if g["date"] == on and g["home"] and g["away"]
        ]
        log.info("NBA slate: %d games", len(out))
        return out

    # -- game logs ---------------------------------------------------------

    def _recent_game_ids(self, team: str, before: date) -> list[tuple[str, date]]:
        games = [
            (g["gameId"], g["date"])
            for g in self._load_schedule()
            if g["date"] < before and team in (g["home"], g["away"])
        ]
        games.sort(key=lambda x: x[1], reverse=True)
        return games[: self.games_back]

    def _boxscore(self, game_id: str) -> dict | None:
        # Final box scores never change, so cache for a year.
        return self.client.get_json(
            BOXSCORE_URL.format(gid=game_id),
            headers=NBA_HEADERS,
            cache_ttl=31_536_000,
            ttl_tag=f"box-{game_id}",
        )

    def players(self, matchups: list[Matchup]) -> list[tuple[PlayerSeason, Matchup]]:
        today = date.today()
        collected: dict[str, PlayerSeason] = {}
        player_game: dict[str, Matchup] = {}

        for m in matchups:
            for team in (m.home_team, m.away_team):
                for game_id, gdate in self._recent_game_ids(team, today):
                    box = self._boxscore(game_id)
                    if not box:
                        continue
                    game = box.get("game", {})
                    for side in ("homeTeam", "awayTeam"):
                        side_data = game.get(side) or {}
                        if side_data.get("teamTricode") != team:
                            continue
                        opp = game.get(
                            "awayTeam" if side == "homeTeam" else "homeTeam", {}
                        ).get("teamTricode", "")
                        for p in side_data.get("players", []):
                            self._add_player_game(
                                collected, p, team, gdate, opp,
                                side == "homeTeam"
                            )
                for name, ps in collected.items():
                    if ps.team == team:
                        player_game.setdefault(name, m)

        out: list[tuple[PlayerSeason, Matchup]] = []
        for name, ps in collected.items():
            ps.logs.sort(key=lambda g: g.game_date, reverse=True)
            m = player_game.get(name)
            if m and len(ps.logs) >= 6:
                out.append((ps, m))

        log.info("NBA players: %d", len(out))
        return out

    def _add_player_game(
        self, collected: dict[str, PlayerSeason], p: dict, team: str,
        gdate: date, opponent: str, home: bool
    ) -> None:
        st = p.get("statistics") or {}
        mins = _minutes(st.get("minutes"))
        if mins <= 0:
            return  # DNP
        name = p.get("name") or f"{p.get('firstName','')} {p.get('familyName','')}".strip()
        key = f"{team}:{name}"
        pts = float(st.get("points", 0) or 0)
        reb = float(st.get("reboundsTotal", 0) or 0)
        ast = float(st.get("assists", 0) or 0)
        stats = {
            "points": pts,
            "rebounds": reb,
            "assists": ast,
            "pra": pts + reb + ast,
            "threes": float(st.get("threePointersMade", 0) or 0),
        }
        ps = collected.get(key)
        if ps is None:
            ps = PlayerSeason(
                player_id=str(p.get("personId", key)),
                name=name,
                team=team,
                sport="NBA",
                position=p.get("position", "") or "",
                logs=[],
            )
            collected[key] = ps
        if any(g.game_date == gdate for g in ps.logs):
            return
        ps.logs.append(
            GameLogEntry(game_date=gdate, opponent=opponent, home=home,
                         stats=stats, minutes=mins)
        )

    def short_rest(self, player: PlayerSeason, on: date) -> bool:
        if not player.logs:
            return False
        return (on - player.logs[0].game_date) <= timedelta(days=1)
