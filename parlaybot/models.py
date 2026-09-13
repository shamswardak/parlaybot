"""Core data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .odds import (
    american_to_decimal,
    format_american,
    prob_to_american,
)


@dataclass
class GameLogEntry:
    """One player's line from one game, reduced to the stats we bet on."""

    game_date: date
    opponent: str
    home: bool
    stats: dict[str, float]
    minutes: float | None = None  # playing-time proxy; used as a volatility filter


@dataclass
class PlayerSeason:
    """Everything we know about one player heading into today."""

    player_id: str
    name: str
    team: str
    sport: str
    position: str = ""
    logs: list[GameLogEntry] = field(default_factory=list)  # newest first

    def values(self, stat: str, limit: int | None = None) -> list[float]:
        logs = self.logs if limit is None else self.logs[:limit]
        return [g.stats[stat] for g in logs if stat in g.stats]


@dataclass
class Matchup:
    """A game on today's slate."""

    game_id: str
    sport: str
    home_team: str
    away_team: str
    start_time: str = ""

    @property
    def label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"


@dataclass
class Leg:
    """A candidate parlay leg: one player, one market, one alternate threshold."""

    sport: str
    game_id: str
    game_label: str
    player: str
    team: str
    market: str            # e.g. "Points", "Total Bases"
    threshold: float       # "20+" -> 20
    stat_key: str

    raw_hit_rate: float    # unweighted hit rate over the trend window
    model_prob: float      # shrunk, recency-weighted, context-adjusted
    est_price: float       # estimated American odds
    n_window: int
    streak: int            # consecutive games hit, most recent first
    window_record: str     # "8/8 L8 - 13/15 L15 - 22/26 season"
    confidence: float      # 0-1, drives ordering and filtering
    notes: list[str] = field(default_factory=list)

    @property
    def decimal(self) -> float:
        return american_to_decimal(self.est_price)

    @property
    def selection(self) -> str:
        return f"{self.player} {self.threshold:g}+ {self.market}"

    @property
    def price_str(self) -> str:
        return format_american(self.est_price)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sport": self.sport,
            "game": self.game_label,
            "player": self.player,
            "team": self.team,
            "market": self.market,
            "threshold": self.threshold,
            "selection": self.selection,
            "est_price": self.est_price,
            "model_prob": round(self.model_prob, 4),
            "raw_hit_rate": round(self.raw_hit_rate, 4),
            "record": self.window_record,
            "streak": self.streak,
            "confidence": round(self.confidence, 3),
            "notes": self.notes,
        }


@dataclass
class Parlay:
    """An assembled ticket."""

    name: str
    legs: list[Leg]
    slate_date: date

    @property
    def total_decimal(self) -> float:
        total = 1.0
        for leg in self.legs:
            total *= leg.decimal
        return total

    @property
    def total_american(self) -> float:
        return prob_to_american(1.0 / self.total_decimal)

    @property
    def model_probability(self) -> float:
        p = 1.0
        for leg in self.legs:
            p *= leg.model_prob
        return p

    @property
    def implied_probability(self) -> float:
        return 1.0 / self.total_decimal

    @property
    def expected_value(self) -> float:
        """Per 1 unit staked, using the model's own probabilities."""
        return self.model_probability * self.total_decimal

    @property
    def sgp_legs(self) -> list[Leg]:
        seen: dict[str, int] = {}
        for leg in self.legs:
            seen[leg.game_id] = seen.get(leg.game_id, 0) + 1
        return [leg for leg in self.legs if seen[leg.game_id] > 1]

    @property
    def n_games(self) -> int:
        return len({leg.game_id for leg in self.legs})

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "date": self.slate_date.isoformat(),
            "n_legs": len(self.legs),
            "n_games": self.n_games,
            "total_american": self.total_american,
            "total_decimal": round(self.total_decimal, 3),
            "model_probability": round(self.model_probability, 5),
            "implied_probability": round(self.implied_probability, 5),
            "expected_value": round(self.expected_value, 4),
            "legs": [leg.to_dict() for leg in self.legs],
        }
