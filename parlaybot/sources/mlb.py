"""MLB via the public MLB StatsAPI (statsapi.mlb.com) -- no key, no quota."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from ..models import GameLogEntry, Matchup, PlayerSeason
from .base import SportSource

log = logging.getLogger(__name__)

BASE = "https://statsapi.mlb.com/api/v1"

HITTER_MARKETS = {
    "hits": {"label": "Hits", "step": 1},
    "total_bases": {"label": "Total Bases", "step": 1},
    "hrr": {"label": "Hits+Runs+RBI", "step": 1},
}

PITCHER_MARKETS = {
    "strikeouts": {"label": "Strikeouts", "step": 1},
    "outs": {"label": "Outs Recorded", "step": 1},
}


def _parse_ip(ip: str | float | None) -> float:
    """MLB innings pitched is '6.1' meaning 6 and 1/3. Convert to outs."""
    if ip is None:
        return 0.0
    s = str(ip)
    if "." not in s:
        return float(s) * 3
    whole, frac = s.split(".", 1)
    return float(whole) * 3 + float(frac[0])


class MLBSource(SportSource):
    sport = "MLB"
    markets = {**HITTER_MARKETS, **PITCHER_MARKETS}

    def __init__(self, client, max_hitters_per_team: int = 7,
                 lead_minutes: int = 20) -> None:
        super().__init__(client, lead_minutes)
        self.max_hitters_per_team = max_hitters_per_team
        self._team_abbr: dict[int, str] = {}
        self._team_of_player: dict[str, str] = {}
        self._is_pitcher: set[str] = set()

    # -- teams -------------------------------------------------------------

    def _load_teams(self) -> None:
        if self._team_abbr:
            return
        data = self.client.get_json(
            f"{BASE}/teams", {"sportId": 1}, cache_ttl=86400, ttl_tag="teams"
        )
        for t in (data or {}).get("teams", []):
            self._team_abbr[t["id"]] = t.get("abbreviation") or t.get("teamName", "")

    # -- slate -------------------------------------------------------------

    def slate(self, on: date) -> list[Matchup]:
        self._load_teams()
        data = self.client.get_json(
            f"{BASE}/schedule",
            {
                "sportId": 1,
                "date": on.isoformat(),
                "hydrate": "probablePitcher,lineups",
            },
            cache_ttl=1800,
            ttl_tag=f"sched-{on}",
        )
        out: list[Matchup] = []
        skipped = 0
        for day in (data or {}).get("dates", []):
            for g in day.get("games", []):
                status = g.get("status", {})
                # abstractGameState is Preview / Live / Final; detailedState
                # carries Postponed, Suspended, Delayed and friends.
                if status.get("abstractGameState") != "Preview":
                    skipped += 1
                    continue
                if not self.is_bettable(g.get("gameDate"),
                                        status.get("detailedState", "")):
                    skipped += 1
                    continue
                home = g["teams"]["home"]["team"]
                away = g["teams"]["away"]["team"]
                m = Matchup(
                    game_id=f"MLB-{g['gamePk']}",
                    sport="MLB",
                    home_team=self._team_abbr.get(home["id"], home.get("name", "")),
                    away_team=self._team_abbr.get(away["id"], away.get("name", "")),
                    start_time=g.get("gameDate", ""),
                )
                m._raw = g  # type: ignore[attr-defined]
                out.append(m)
        self.last_skipped = skipped
        log.info("MLB slate: %d bettable games (%d already started or unavailable)",
                 len(out), skipped)
        return out

    def stale_teams(self, on: date) -> set[str]:
        """Teams whose previous day's game is not yet final."""
        self._load_teams()
        yesterday = on - timedelta(days=1)
        data = self.client.get_json(
            f"{BASE}/schedule",
            {"sportId": 1, "date": yesterday.isoformat()},
            cache_ttl=900,
            ttl_tag=f"sched-prev-{yesterday}",
        )
        stale: set[str] = set()
        for day in (data or {}).get("dates", []):
            for g in day.get("games", []):
                state = g.get("status", {}).get("abstractGameState")
                if state == "Final":
                    continue
                for side in ("home", "away"):
                    team = g.get("teams", {}).get(side, {}).get("team", {})
                    abbr = self._team_abbr.get(team.get("id"))
                    if abbr:
                        stale.add(abbr)
        if stale:
            log.info("MLB: %d team(s) have an unfinished %s game — their trend "
                     "windows are a game behind: %s",
                     len(stale), yesterday, ", ".join(sorted(stale)))
        return stale

    # -- players -----------------------------------------------------------

    def _roster_hitters(self, team_id: int, season: int) -> list[dict]:
        data = self.client.get_json(
            f"{BASE}/teams/{team_id}/roster/Active",
            {
                "hydrate": f"person(stats(type=season,season={season},group=hitting))"
            },
            cache_ttl=21600,
            ttl_tag=f"roster-{team_id}-{season}",
        )
        rows: list[dict] = []
        for entry in (data or {}).get("roster", []):
            person = entry.get("person", {})
            pos = entry.get("position", {}).get("abbreviation", "")
            if pos == "P":
                continue
            abs_ = 0
            for block in person.get("stats", []):
                for split in block.get("splits", []):
                    abs_ = max(abs_, int(split.get("stat", {}).get("atBats", 0) or 0))
            rows.append({"id": person.get("id"), "name": person.get("fullName"),
                         "pos": pos, "at_bats": abs_})
        rows.sort(key=lambda r: -r["at_bats"])
        return rows[: self.max_hitters_per_team]

    def _lineup_ids(self, matchup: Matchup, side: str) -> list[dict]:
        raw = getattr(matchup, "_raw", {})
        lineups = raw.get("lineups") or {}
        players = lineups.get(f"{side}Players") or []
        return [{"id": p.get("id"), "name": p.get("fullName"), "pos": "", "at_bats": 999}
                for p in players if p.get("id")]

    def players(self, matchups: list[Matchup]) -> list[tuple[PlayerSeason, Matchup]]:
        season = datetime.now().year
        out: list[tuple[PlayerSeason, Matchup]] = []

        for m in matchups:
            raw = getattr(m, "_raw", {})
            for side, team_abbr in (("home", m.home_team), ("away", m.away_team)):
                team_id = raw.get("teams", {}).get(side, {}).get("team", {}).get("id")
                if not team_id:
                    continue

                # Confirmed lineup beats a guess at the top of the order.
                hitters = self._lineup_ids(m, side)
                if not hitters:
                    hitters = self._roster_hitters(team_id, season)

                for h in hitters:
                    ps = self._player_season(h["id"], h["name"], team_abbr, "hitting",
                                             season)
                    if ps:
                        out.append((ps, m))

                pitcher = (
                    raw.get("teams", {}).get(side, {}).get("probablePitcher") or {}
                )
                if pitcher.get("id"):
                    ps = self._player_season(
                        pitcher["id"], pitcher.get("fullName", ""), team_abbr,
                        "pitching", season
                    )
                    if ps:
                        self._is_pitcher.add(str(pitcher["id"]))
                        out.append((ps, m))

        log.info("MLB players: %d", len(out))
        return out

    def _player_season(
        self, player_id: int, name: str, team: str, group: str, season: int
    ) -> PlayerSeason | None:
        data = self.client.get_json(
            f"{BASE}/people/{player_id}/stats",
            {"stats": "gameLog", "group": group, "season": season},
            # One hour, not six: a six-hour-old log misses every game
            # played since, which is exactly how a broken streak gets
            # quoted as live.
            cache_ttl=3600,
            ttl_tag=f"log-{player_id}-{group}-{season}",
        )
        splits = []
        for block in (data or {}).get("stats", []):
            splits.extend(block.get("splits", []))
        if not splits:
            return None

        logs: list[GameLogEntry] = []
        for sp in splits:
            st = sp.get("stat", {})
            try:
                gd = datetime.strptime(sp["date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue

            if group == "hitting":
                hits = float(st.get("hits", 0) or 0)
                doubles = float(st.get("doubles", 0) or 0)
                triples = float(st.get("triples", 0) or 0)
                hr = float(st.get("homeRuns", 0) or 0)
                singles = hits - doubles - triples - hr
                tb = singles + 2 * doubles + 3 * triples + 4 * hr
                runs = float(st.get("runs", 0) or 0)
                rbi = float(st.get("rbi", 0) or 0)
                stats = {
                    "hits": hits,
                    "total_bases": tb,
                    "hrr": hits + runs + rbi,
                }
                playing_time = float(st.get("plateAppearances", 0) or 0)
            else:
                stats = {
                    "strikeouts": float(st.get("strikeOuts", 0) or 0),
                    "outs": _parse_ip(st.get("inningsPitched")),
                }
                playing_time = stats["outs"]

            logs.append(
                GameLogEntry(
                    game_date=gd,
                    opponent=sp.get("opponent", {}).get("name", ""),
                    home=sp.get("isHome", False),
                    stats=stats,
                    minutes=playing_time or None,
                )
            )

        logs.sort(key=lambda g: g.game_date, reverse=True)
        return PlayerSeason(
            player_id=str(player_id),
            name=name,
            team=team,
            sport="MLB",
            position="P" if group == "pitching" else "",
            logs=logs,
        )

    def markets_for(self, player: PlayerSeason) -> dict[str, dict]:
        return PITCHER_MARKETS if player.position == "P" else HITTER_MARKETS

    def short_rest(self, player: PlayerSeason, on: date) -> bool:
        # Baseball position players play daily; only starting pitchers have a
        # meaningful rest signal, and a probable starter is rested by definition.
        return False

    def actual(self, player_id: str, stat_key: str, on: date) -> float | None:
        group = "pitching" if stat_key in PITCHER_MARKETS else "hitting"
        season = on.year
        player = self._player_season(int(player_id), "", "", group, season)
        return self._from_logs(player, stat_key, on)
