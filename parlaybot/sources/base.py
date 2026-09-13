"""Source interface.

Each sport implements `slate()` -> today's games, and `players()` -> the
PlayerSeason objects worth pricing for those games. Markets are declared per
sport as stat_key -> {label, step}, where `step` is the granularity of the
alternate-line ladder (1 rebound, 5 receiving yards, and so on).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..http import HttpClient
from ..models import Matchup, PlayerSeason


class SportSource(ABC):
    sport: str = ""
    markets: dict[str, dict] = {}

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    @abstractmethod
    def slate(self, on: date) -> list[Matchup]:
        """Games scheduled on `on`."""

    @abstractmethod
    def players(self, matchups: list[Matchup]) -> list[tuple[PlayerSeason, Matchup]]:
        """Players expected to feature, paired with their game."""

    def short_rest(self, player: PlayerSeason, on: date) -> bool:
        """True when the player's last game was within a day (B2B) -- the single
        biggest trend-breaker in the daily sports."""
        if not player.logs:
            return False
        return (on - player.logs[0].game_date).days <= 1

    def is_home(self, player: PlayerSeason, matchup: Matchup) -> bool:
        return player.team == matchup.home_team

    def actual(self, player_id: str, stat_key: str, on: date) -> float | None:
        """What the player actually recorded for `stat_key` on `on`.

        None means "no result": the player didn't appear, the game was
        postponed, or the upstream has no row. The grader treats that as a void
        leg rather than a loss, which is how a sportsbook would settle it.

        Sports that can answer override this; the base returns None so a sport
        without result lookup degrades to "ungraded" instead of scoring wrong.
        """
        return None

    @staticmethod
    def _from_logs(player: PlayerSeason | None, stat_key: str,
                   on: date) -> float | None:
        """Pull one stat for one date out of a fetched game log.

        Doubleheaders produce two entries for the same date; books settle most
        props on the first game, so that is what this returns.
        """
        if player is None:
            return None
        for entry in reversed(player.logs):  # logs are newest-first
            if entry.game_date == on:
                return entry.stats.get(stat_key)
        return None
