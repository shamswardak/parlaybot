"""Real sportsbook odds from The Odds API.

The free tier is 500 credits a month and credits are charged as
[markets] x [regions] per event request. Fetching one market across a 15-game
MLB slate is 15 credits a day -- 450 a month, inside the free allowance. So the
budget is real but workable, provided nothing wastes it.

Three things protect the budget, because the failure mode here is silent: you
don't notice you've burned the month until the tickets stop having prices.

1.  ETag revalidation. The API charges ZERO credits for a 304 Not Modified, so
    every response's ETag is stored and sent back on the next request. Re-runs
    that land on unchanged odds cost nothing at all.
2.  A disk cache with a TTL, so repeated runs inside the same window don't even
    reach the network.
3.  A hard monthly budget. The client counts what it spends, reads the credits
    the API reports remaining, and refuses to start a request that would break
    the cap -- degrading to modelled prices rather than failing.

With all three, the daily scheduled run costs its 15 credits, and manual runs
the same day are free.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4"

SPORT_KEYS = {
    "MLB": "baseball_mlb",
    "NFL": "americanfootball_nfl",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
}

# Their market key -> our stat key. The `_alternate` variants are the milestone
# ("3+ strikeouts") ladders; the plain keys are single over/under lines.
MARKET_MAP = {
    "MLB": {
        "pitcher_strikeouts_alternate": "strikeouts",
        "pitcher_outs_alternate": "outs",
        "batter_hits_alternate": "hits",
        "batter_total_bases_alternate": "total_bases",
        "batter_hits_runs_rbis_alternate": "hrr",
    },
    "NFL": {
        "player_pass_yds_alternate": "passing_yards",
        "player_rush_yds_alternate": "rushing_yards",
        "player_reception_yds_alternate": "receiving_yards",
        "player_receptions_alternate": "receptions",
        "player_pass_completions_alternate": "completions",
    },
    "NBA": {
        "player_points_alternate": "points",
        "player_rebounds_alternate": "rebounds",
        "player_assists_alternate": "assists",
        "player_threes_alternate": "threes",
    },
    "NHL": {
        "player_shots_on_goal_alternate": "shots",
        "player_points_alternate": "points",
    },
}


@dataclass
class PropOffer:
    """One real, placeable line from one book."""

    sport: str
    event_id: str
    home_team: str
    away_team: str
    commence_time: str
    book: str
    stat_key: str
    market_key: str
    player: str
    threshold: float
    price: float
    link: str = ""

    @property
    def game_label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"


@dataclass
class Budget:
    """Monthly credit accounting, persisted between runs."""

    limit: int = 450
    used: int = 0
    month: str = ""
    remaining_reported: int | None = None

    def rollover(self, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
        if self.month != stamp:
            self.month, self.used = stamp, 0

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    def can_spend(self, credits: int) -> bool:
        return self.used + credits <= self.limit


class OddsClient:
    def __init__(
        self,
        api_key: str,
        cache_dir: str | Path = ".cache/odds",
        state_path: str | Path = "history/odds_budget.json",
        monthly_limit: int = 450,
        cache_ttl: float = 3600.0,
        books: tuple[str, ...] = ("draftkings", "fanduel"),
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(state_path)
        self.cache_ttl = cache_ttl
        self.books = books
        self.timeout = timeout
        self.session = requests.Session()
        self.budget = self._load_budget(monthly_limit)
        self.spent_this_run = 0
        self.free_revalidations = 0

    # -- budget state ------------------------------------------------------

    def _load_budget(self, limit: int) -> Budget:
        budget = Budget(limit=limit)
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                budget = Budget(
                    limit=limit,
                    used=int(data.get("used", 0)),
                    month=str(data.get("month", "")),
                    remaining_reported=data.get("remaining_reported"),
                )
            except (OSError, json.JSONDecodeError, ValueError):
                log.warning("could not read odds budget state; starting fresh")
        budget.rollover()
        return budget

    def save_budget(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_path.write_text(json.dumps({
                "month": self.budget.month,
                "used": self.budget.used,
                "limit": self.budget.limit,
                "remaining_reported": self.budget.remaining_reported,
            }, indent=2))
        except OSError:
            log.warning("could not persist odds budget state")

    # -- HTTP with ETag revalidation ---------------------------------------

    def _cache_paths(self, key: str) -> tuple[Path, Path]:
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.etag"

    def _get(self, url: str, params: dict, cost: int, cache_key: str):
        body_path, etag_path = self._cache_paths(cache_key)

        # Fresh enough on disk: no request, no credit.
        if body_path.exists():
            age = time.time() - body_path.stat().st_mtime
            if age < self.cache_ttl:
                try:
                    return json.loads(body_path.read_text())
                except (OSError, json.JSONDecodeError):
                    pass

        if not self.budget.can_spend(cost):
            log.warning(
                "odds budget exhausted (%d/%d used this month) — skipping %s",
                self.budget.used, self.budget.limit, cache_key,
            )
            return None

        headers = {}
        if etag_path.exists():
            try:
                headers["If-None-Match"] = etag_path.read_text().strip()
            except OSError:
                pass

        try:
            resp = self.session.get(
                url, params={**params, "apiKey": self.api_key},
                headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            log.warning("odds request failed for %s: %s", cache_key, exc)
            return None

        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            try:
                self.budget.remaining_reported = int(float(remaining))
            except ValueError:
                pass

        # 304: the odds haven't moved. Free, per the API's own accounting.
        if resp.status_code == 304:
            self.free_revalidations += 1
            log.debug("304 for %s — no credit charged", cache_key)
            if body_path.exists():
                try:
                    body_path.touch()
                    return json.loads(body_path.read_text())
                except (OSError, json.JSONDecodeError):
                    return None
            return None

        if resp.status_code == 401:
            log.error("odds API rejected the key (401)")
            return None
        if resp.status_code == 422:
            log.warning("odds API rejected the request for %s (422): %s",
                        cache_key, resp.text[:200])
            return None
        if resp.status_code != 200:
            log.warning("odds API returned %s for %s", resp.status_code, cache_key)
            return None

        self.budget.used += cost
        self.spent_this_run += cost

        try:
            data = resp.json()
        except ValueError:
            return None

        try:
            body_path.write_text(json.dumps(data))
            if resp.headers.get("ETag"):
                etag_path.write_text(resp.headers["ETag"])
        except OSError:
            pass
        return data

    # -- endpoints ---------------------------------------------------------

    def events(self, sport: str) -> list[dict]:
        """Upcoming events. Costs 1 credit."""
        key = SPORT_KEYS.get(sport)
        if not key:
            return []
        data = self._get(f"{BASE}/sports/{key}/events", {}, 1, f"events-{sport}")
        return data or []

    def event_offers(
        self, sport: str, event: dict, markets: list[str], regions: str = "us"
    ) -> list[PropOffer]:
        """Player props for one event. Costs [markets] x [regions] credits."""
        key = SPORT_KEYS.get(sport)
        if not key or not markets:
            return []
        event_id = event.get("id", "")
        cost = len(markets) * len(regions.split(","))
        data = self._get(
            f"{BASE}/sports/{key}/events/{event_id}/odds",
            {
                "regions": regions,
                "markets": ",".join(markets),
                "oddsFormat": "american",
                "bookmakers": ",".join(self.books),
                "includeLinks": "true",
            },
            cost,
            f"odds-{sport}-{event_id}-{'_'.join(sorted(markets))}",
        )
        if not data:
            return []
        return parse_event(sport, data)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def threshold_from_point(point: float) -> float:
    """An X+ milestone is posted as Over (X - 0.5).

    "5+ strikeouts" arrives as Over 4.5, "50+ rushing yards" as Over 49.5, so
    rounding up recovers the number a human would recognise on the ticket.
    """
    return float(math.ceil(point))


def parse_event(sport: str, payload: dict) -> list[PropOffer]:
    mapping = MARKET_MAP.get(sport, {})
    offers: list[PropOffer] = []

    home = payload.get("home_team", "")
    away = payload.get("away_team", "")
    event_id = payload.get("id", "")
    commence = payload.get("commence_time", "")

    for book in payload.get("bookmakers", []):
        book_key = book.get("key", "")
        for market in book.get("markets", []):
            market_key = market.get("key", "")
            stat_key = mapping.get(market_key)
            if not stat_key:
                continue
            for outcome in market.get("outcomes", []):
                # Only the Over side of a milestone ladder is a "X+" leg.
                if str(outcome.get("name", "")).lower() != "over":
                    continue
                player = outcome.get("description") or ""
                point = outcome.get("point")
                price = outcome.get("price")
                if not player or point is None or price is None:
                    continue
                offers.append(PropOffer(
                    sport=sport,
                    event_id=event_id,
                    home_team=home,
                    away_team=away,
                    commence_time=commence,
                    book=book_key,
                    stat_key=stat_key,
                    market_key=market_key,
                    player=str(player),
                    threshold=threshold_from_point(float(point)),
                    price=float(price),
                    link=outcome.get("link") or market.get("link") or "",
                ))
    return offers


def best_offers(offers: list[PropOffer]) -> list[PropOffer]:
    """Best available price per (player, market, threshold) across books.

    Shopping the better of two books is the one edge available for free, so
    where DraftKings and FanDuel both post a line, take the longer price.
    """
    best: dict[tuple, PropOffer] = {}
    for offer in offers:
        key = (offer.player, offer.stat_key, offer.threshold)
        current = best.get(key)
        # American odds: larger is always better for the bettor once compared
        # as implied probability, so compare that way rather than on sign.
        if current is None or _implied(offer.price) < _implied(current.price):
            best[key] = offer
    return list(best.values())


def _implied(american: float) -> float:
    return (-american / (-american + 100)) if american < 0 else (100 / (american + 100))
