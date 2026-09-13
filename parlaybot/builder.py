"""Ticket assembly from per-ticket criteria.

There is no payout target. A ticket is correct when every leg on it meets that
ticket's criteria -- a price range, optionally a streak requirement, optionally
a restriction on which sports and how much current-season sample a leg needs.
Whatever the legs multiply out to is the result, not the goal.

Three tickets by default, each a different bet:

  Safe    20 legs around -900. The price is the safety, so this one does not
          require current-season sample -- a near-certain low threshold holds
          up in Week 1 as well as Week 12. Trends break ties, nothing more.
  Core    10 legs at -500/-650, current-season form required.
  Trend    5 legs at -150/-400, and here the streak is the point: a player has
          to have done it several games running to qualify.

When a slate cannot fill a ticket, the criteria loosen a step at a time -- the
price band widens, the streak requirement drops -- and the ticket says which
pass produced it. A relaxed ticket is better than no ticket, as long as it is
honest about being relaxed.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .models import Leg, Parlay
from .odds import american_to_prob, format_american, prob_to_american

log = logging.getLogger(__name__)


@dataclass
class TicketSpec:
    name: str
    n_legs: int
    price_min: float              # most negative edge, e.g. -1200
    price_max: float              # least negative edge, e.g. -800
    sports: list[str] | None = None        # None = every enabled sport
    min_streak: int = 0
    require_current_season: bool = True
    max_legs_per_game: int = 2
    absolute_min_legs: int = 3
    relax_steps: int = 5
    relax_prob: float = 0.02      # band widening per pass, in implied probability
    relax_streak: int = 1
    # Steps spent easing the streak requirement BEFORE the price band moves.
    # The band is what gives a ticket its identity -- a 5-leg trend ticket that
    # widens its way up to -900 has quietly become the safe ticket -- so the
    # streak gives first.
    streak_relax_steps: int = 2

    def band_at(self, step: int) -> tuple[float, float]:
        """The price band after `step` relaxation passes.

        Widened in probability space rather than American points: -900 to -1000
        is a far smaller move than -200 to -300, so widening by a fixed number
        of points would loosen the long end drastically and the short end
        barely at all.
        """
        widen = max(0, step - self._streak_budget)
        lo_p = american_to_prob(self.price_min) + widen * self.relax_prob
        hi_p = american_to_prob(self.price_max) - widen * self.relax_prob
        lo_p = min(lo_p, 0.985)
        hi_p = max(hi_p, 0.10)
        # Returned as (most negative, least negative), matching price_min/max.
        return prob_to_american(lo_p), prob_to_american(hi_p)

    @property
    def _streak_budget(self) -> int:
        """Relaxation steps reserved for the streak, zero when none is required."""
        return self.streak_relax_steps if self.min_streak else 0

    def streak_at(self, step: int) -> int:
        used = min(step, self._streak_budget)
        return max(0, self.min_streak - used * self.relax_streak)


DEFAULT_SPECS = [
    TicketSpec(name="Safe 20", n_legs=20, price_min=-1400, price_max=-800,
               sports=None, min_streak=0, require_current_season=False),
    TicketSpec(name="Core 10", n_legs=10, price_min=-700, price_max=-450,
               sports=["MLB"], min_streak=0, require_current_season=True),
    TicketSpec(name="Trend 5", n_legs=5, price_min=-400, price_max=-150,
               sports=["MLB"], min_streak=5, require_current_season=True),
]


# --------------------------------------------------------------------------
# Filtering and selection
# --------------------------------------------------------------------------

def eligible(
    legs: list[Leg],
    spec: TicketSpec,
    band: tuple[float, float],
    min_streak: int,
    min_season_games: dict[str, int] | None,
) -> list[Leg]:
    lo, hi = min(band), max(band)
    out = []
    for leg in legs:
        if spec.sports and leg.sport not in spec.sports:
            continue
        if not (lo <= leg.est_price <= hi):
            continue
        if leg.streak < min_streak:
            continue
        if spec.require_current_season:
            if leg.prior_season_only:
                continue
            floor = (min_season_games or {}).get(leg.sport, 0)
            if leg.current_games < floor:
                continue
        out.append(leg)
    return out


def best_per_player(legs: list[Leg], band: tuple[float, float]) -> list[Leg]:
    """One leg per player: the strongest opinion, not the longest ladder.

    Ranked by streak first -- the user's stated preference for trend legs over
    nominal favourites -- then confidence, then proximity to the middle of the
    band, so a ticket doesn't cluster at one edge of its price range.
    """
    centre = (american_to_prob(min(band)) + american_to_prob(max(band))) / 2

    def rank(leg: Leg):
        return (leg.streak, leg.confidence,
                -abs(american_to_prob(leg.est_price) - centre))

    best: dict[str, Leg] = {}
    for leg in legs:
        current = best.get(leg.player)
        if current is None or rank(leg) > rank(current):
            best[leg.player] = leg
    return sorted(best.values(), key=rank, reverse=True)


def select(
    legs: list[Leg], n_legs: int, max_per_game: int, exclude_players: set[str]
) -> list[Leg]:
    chosen: list[Leg] = []
    per_game: dict[str, int] = defaultdict(int)
    used: set[str] = set()

    # One pass per game-cap level, so every distinct game is used before any
    # game is doubled up.
    for cap in range(1, max_per_game + 1):
        for leg in legs:
            if len(chosen) >= n_legs:
                return chosen
            if leg.player in used or leg.player in exclude_players:
                continue
            if per_game[leg.game_id] >= cap:
                continue
            chosen.append(leg)
            used.add(leg.player)
            per_game[leg.game_id] += 1
    return chosen


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def build_ticket(
    legs: list[Leg],
    spec: TicketSpec,
    slate_date: date,
    exclude_players: set[str] | None = None,
    min_season_games: dict[str, int] | None = None,
) -> Parlay | None:
    """Fill one ticket, loosening the criteria a step at a time if short."""
    exclude_players = exclude_players or set()
    best: tuple[int, list[Leg], int] | None = None

    for step in range(spec.relax_steps + 1):
        band = spec.band_at(step)
        streak = spec.streak_at(step)
        pool = eligible(legs, spec, band, streak, min_season_games)
        chosen = select(best_per_player(pool, band), spec.n_legs,
                        spec.max_legs_per_game, exclude_players)

        if best is None or len(chosen) > best[0]:
            best = (len(chosen), chosen, step)
        if len(chosen) >= spec.n_legs:
            break

    if best is None or len(best[1]) < spec.absolute_min_legs:
        return None

    count, chosen, step = best
    parlay = Parlay(name=spec.name, legs=list(chosen), slate_date=slate_date)

    band = spec.band_at(step)
    if step > 0:
        parlay.notes.append(
            f"Criteria loosened {step} step(s) to fill this: price "
            f"{format_american(max(band))} to {format_american(min(band))}"
            + (f", streak {spec.streak_at(step)}+ instead of {spec.min_streak}+"
               if spec.min_streak else "")
        )
    if count < spec.n_legs:
        parlay.notes.append(
            f"{count} legs, not {spec.n_legs} — the slate ran out of legs "
            f"meeting this ticket's criteria even after loosening."
        )

    per_game: dict[str, int] = defaultdict(int)
    for leg in parlay.legs:
        per_game[leg.game_id] += 1
    biggest = max(per_game.values()) if per_game else 0
    if biggest >= 3 and biggest >= len(parlay.legs) / 2:
        label = next(l.game_label for l in parlay.legs
                     if per_game[l.game_id] == biggest)
        parlay.notes.append(
            f"{biggest} of {len(parlay.legs)} legs are in one game ({label}). "
            f"The book prices that as a same-game parlay, so the real payout "
            f"will be under the number above."
        )

    return parlay


def build_slate(
    legs: list[Leg],
    specs: list[TicketSpec],
    slate_date: date,
    min_season_games: dict[str, int] | None = None,
    share_players: bool = False,
) -> list[Parlay]:
    """Build every ticket for the day.

    Tickets don't share players by default: the same hot bat appearing on all
    three turns them into one correlated bet wearing three hats.
    """
    out: list[Parlay] = []
    used: set[str] = set()
    for spec in specs:
        ticket = build_ticket(
            legs, spec, slate_date,
            exclude_players=set() if share_players else set(used),
            min_season_games=min_season_games,
        )
        if ticket is None:
            log.warning("could not build %s", spec.name)
            continue
        out.append(ticket)
        if not share_players:
            used.update(leg.player for leg in ticket.legs)
    return out
