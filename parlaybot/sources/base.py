"""Source interface.

Each sport implements `slate()` -> today's games, and `players()` -> the
PlayerSeason objects worth pricing for those games. Markets are declared per
sport as stat_key -> {label, step}, where `step` is the granularity of the
alternate-line ladder (1 rebound, 5 receiving yards, and so on).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone

from ..http import HttpClient
from ..models import Matchup, PlayerSeason

log = logging.getLogger(__name__)

# Statuses that mean the game is under way or done with. Anything here is not
# bettable at pregame prices, whatever the schedule date says.
DEAD_STATES = {
    "live", "in progress", "final", "game over", "completed", "postponed",
    "suspended", "cancelled", "canceled", "delayed", "off", "crit", "fut-off",
}


def parse_utc(value: str | None) -> datetime | None:
    """Parse the ISO timestamps these APIs hand back, normalised to UTC."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SportSource(ABC):
    sport: str = ""
    markets: dict[str, dict] = {}

    def __init__(self, client: HttpClient, lead_minutes: int = 20) -> None:
        self.client = client
        # Don't offer a game that has started, or is about to. A leg on a game
        # already in progress can't be bet at the price the model estimated,
        # and may be half-decided already.
        self.lead_minutes = lead_minutes
        # How many of today's games slate() dropped as already under way. Lets
        # the report tell "nothing scheduled" apart from "everything started".
        self.last_skipped = 0

    def is_bettable(self, start: str | datetime | None, status: str = "",
                    now: datetime | None = None) -> bool:
        """True when the game hasn't started and there's time to place a bet.

        Both checks matter. Status catches games the API already knows are
        live, and the clock catches the gap where a game has started but the
        feed hasn't updated -- or where it starts in four minutes and there's
        no realistic chance of getting the bet down.
        """
        if status and status.strip().lower() in DEAD_STATES:
            return False

        start_dt = start if isinstance(start, datetime) else parse_utc(start)
        if start_dt is None:
            # No start time: fall back to status alone rather than dropping a
            # game the feed simply didn't timestamp.
            return True

        now = now or datetime.now(timezone.utc)
        return start_dt > now + timedelta(minutes=self.lead_minutes)

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
