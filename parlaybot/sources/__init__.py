from .base import SportSource
from .mlb import MLBSource
from .nba import NBASource
from .nfl import NFLSource
from .nhl import NHLSource

SOURCES = {
    "MLB": MLBSource,
    "NFL": NFLSource,
    "NBA": NBASource,
    "NHL": NHLSource,
}

__all__ = ["SportSource", "MLBSource", "NBASource", "NFLSource", "NHLSource", "SOURCES"]
