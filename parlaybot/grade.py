"""Grade yesterday's tickets against what actually happened.

This is the only part of the system that can tell you whether the model is any
good. Everything upstream is an opinion; this is the scoreboard.

Settlement follows how a sportsbook would treat each leg:

- hit   -- the player reached the threshold
- miss  -- he didn't
- void  -- he didn't play, the game was postponed, or the stat line isn't
           published yet. A book voids the leg and shortens the parlay; it does
           NOT count as a loss, and it is excluded from calibration.

A ticket is a loss the moment one leg misses, a win if every non-void leg hits,
and pending while results are still missing and nothing has missed yet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .http import HttpClient
from .odds import american_to_decimal, decimal_to_american, format_american
from .sources import SOURCES

log = logging.getLogger(__name__)

HIT = "hit"
MISS = "miss"
VOID = "void"


@dataclass
class LegResult:
    leg: dict
    status: str
    actual: float | None

    @property
    def needed(self) -> float:
        return float(self.leg["threshold"])

    @property
    def margin(self) -> float | None:
        """How much the player cleared (or missed) the line by."""
        if self.actual is None:
            return None
        return self.actual - self.needed

    @property
    def label(self) -> str:
        return self.leg.get("selection") or (
            f"{self.leg['player']} {self.leg['threshold']:g}+ {self.leg['market']}"
        )

    def describe(self) -> str:
        if self.status == VOID:
            return f"{self.label} — no result (DNP or not yet published)"
        got = f"{self.actual:g}" if self.actual is not None else "?"
        verb = "got" if self.status == HIT else "only got"
        return f"{self.label} — {verb} {got}, needed {self.needed:g}"


@dataclass
class TicketResult:
    name: str
    slate_date: date
    legs: list[LegResult]
    total_american: float
    model_probability: float

    @property
    def status(self) -> str:
        if any(r.status == MISS for r in self.legs):
            return "LOSS"
        if any(r.status == VOID for r in self.legs):
            return "PENDING"
        return "WIN"

    @property
    def misses(self) -> list[LegResult]:
        return [r for r in self.legs if r.status == MISS]

    @property
    def hits(self) -> list[LegResult]:
        return [r for r in self.legs if r.status == HIT]

    @property
    def voids(self) -> list[LegResult]:
        return [r for r in self.legs if r.status == VOID]

    @property
    def settled_decimal(self) -> float:
        """Payout after voids are removed, as a book would recompute it."""
        total = 1.0
        for r in self.legs:
            if r.status != VOID:
                total *= american_to_decimal(r.leg["est_price"])
        return total

    def near_miss_note(self) -> str | None:
        """One line on how close the ticket came.

        A 20-leg ticket that dies on a single leg by one hit is a very
        different signal from one that dies on six legs, and the distinction
        matters when you're deciding whether the model is close or broken.
        """
        if self.status != "LOSS":
            return None
        misses = self.misses
        if len(misses) == 1:
            m = misses[0]
            by = abs(m.margin) if m.margin is not None else None
            tail = f" by {by:g}" if by is not None else ""
            return f"Died on one leg{tail}: {m.label}"
        return f"Died on {len(misses)} legs"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "date": self.slate_date.isoformat(),
            "status": self.status,
            "n_legs": len(self.legs),
            "hits": len(self.hits),
            "misses": len(self.misses),
            "voids": len(self.voids),
            "total_american": self.total_american,
            "model_probability": self.model_probability,
            "failed_legs": [r.describe() for r in self.misses],
        }


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

def grade_tickets(
    tickets: list[dict], on: date, client: HttpClient | None = None
) -> list[TicketResult]:
    client = client or HttpClient()
    sources: dict[str, Any] = {}
    # One lookup per (sport, player, stat) even when several tickets share a leg.
    cache: dict[tuple[str, str, str], float | None] = {}

    results: list[TicketResult] = []
    for ticket in tickets:
        leg_results: list[LegResult] = []
        for leg in ticket.get("legs", []):
            sport = leg.get("sport", "")
            key = (sport, str(leg.get("player_id", "")), leg.get("stat_key", ""))

            if key in cache:
                actual = cache[key]
            else:
                actual = None
                source_cls = SOURCES.get(sport)
                if source_cls and leg.get("player_id"):
                    source = sources.get(sport)
                    if source is None:
                        source = sources[sport] = source_cls(client)
                    try:
                        actual = source.actual(
                            str(leg["player_id"]), leg["stat_key"], on
                        )
                    except Exception:
                        log.exception("result lookup failed for %s", leg.get("player"))
                        actual = None
                cache[key] = actual

            if actual is None:
                status = VOID
            elif actual >= float(leg["threshold"]):
                status = HIT
            else:
                status = MISS
            leg_results.append(LegResult(leg=leg, status=status, actual=actual))

        results.append(
            TicketResult(
                name=ticket.get("name", "Ticket"),
                slate_date=on,
                legs=leg_results,
                total_american=ticket.get("total_american", 0.0),
                model_probability=ticket.get("model_probability", 0.0),
            )
        )
    return results


# --------------------------------------------------------------------------
# History on disk
# --------------------------------------------------------------------------

def load_tickets(history_dir: str | Path, on: date) -> list[dict] | None:
    path = Path(history_dir) / f"parlays-{on.isoformat()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.exception("could not read %s", path)
        return None


def save_results(history_dir: str | Path, on: date,
                 results: list[TicketResult]) -> Path:
    d = Path(history_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"results-{on.isoformat()}.json"
    path.write_text(json.dumps([r.to_dict() for r in results], indent=2))
    return path


def append_graded_legs(history_dir: str | Path, on: date,
                       results: list[TicketResult]) -> int:
    """Append every settled leg to the calibration ledger.

    One line per leg, deduplicated by (date, player, stat, threshold) so
    re-grading the same day doesn't double-count and skew calibration.
    """
    d = Path(history_dir)
    d.mkdir(parents=True, exist_ok=True)
    ledger = d / "legs.jsonl"

    seen: set[tuple] = set()
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.add((row.get("date"), row.get("player_id"),
                      row.get("stat_key"), row.get("threshold")))

    written = 0
    with ledger.open("a") as fh:
        for ticket in results:
            for r in ticket.legs:
                if r.status == VOID:
                    continue  # a void teaches nothing about calibration
                ident = (on.isoformat(), str(r.leg.get("player_id", "")),
                         r.leg.get("stat_key", ""), r.leg.get("threshold"))
                if ident in seen:
                    continue
                seen.add(ident)
                fh.write(json.dumps({
                    "date": on.isoformat(),
                    "sport": r.leg.get("sport"),
                    # Recorded now so opponent-strength modelling has history to
                    # work with when we build it -- "missed because he drew a
                    # team that defends the three well" needs the opponent on
                    # every settled leg, going back as far as possible.
                    "opponent": r.leg.get("opponent", ""),
                    "game": r.leg.get("game"),
                    "team": r.leg.get("team"),
                    "actual": r.actual,
                    "player_id": str(r.leg.get("player_id", "")),
                    "player": r.leg.get("player"),
                    "stat_key": r.leg.get("stat_key"),
                    "threshold": r.leg.get("threshold"),
                    "market": r.leg.get("market"),
                    "model_prob": r.leg.get("model_prob"),
                    "est_price": r.leg.get("est_price"),
                    "confidence": r.leg.get("confidence"),
                    "streak": r.leg.get("streak"),
                    "hit": r.status == HIT,
                }) + "\n")
                written += 1
    return written


def running_record(history_dir: str | Path) -> dict[str, int]:
    """Ticket-level W/L across every results file on disk."""
    d = Path(history_dir)
    record = {"wins": 0, "losses": 0, "pending": 0, "tickets": 0}
    if not d.exists():
        return record
    for path in sorted(d.glob("results-*.json")):
        try:
            rows = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for row in rows:
            record["tickets"] += 1
            status = row.get("status")
            if status == "WIN":
                record["wins"] += 1
            elif status == "LOSS":
                record["losses"] += 1
            else:
                record["pending"] += 1
    return record


def previous_slate_date(history_dir: str | Path, before: date) -> date | None:
    """Most recent slate with tickets but no results yet."""
    d = Path(history_dir)
    if not d.exists():
        return None
    candidates = []
    for path in d.glob("parlays-*.json"):
        try:
            when = datetime.strptime(path.stem.replace("parlays-", ""),
                                     "%Y-%m-%d").date()
        except ValueError:
            continue
        if when < before:
            candidates.append(when)
    return max(candidates) if candidates else None


def format_settled_price(result: TicketResult) -> str:
    try:
        return format_american(decimal_to_american(result.settled_decimal))
    except ValueError:
        return "n/a"
