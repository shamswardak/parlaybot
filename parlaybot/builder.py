"""Parlay assembly.

Given a pool of candidate legs, build tickets that land near a target payout
using a fixed leg count, preferring one leg per game and falling back to
same-game legs only when the slate is too thin.

The search is a two-stage affair:

1.  Slot selection -- pick which (player, market) ladders go on the ticket,
    ranked by confidence, subject to one leg per player and a cap per game.
2.  Rung selection -- hill-climb the threshold chosen on each ladder so the
    product of the leg prices converges on the target payout, trading as little
    confidence as possible to get there.

Stage 2 is what makes a "target +1500" instruction meaningful without a live
odds feed: the ladders give the builder something to tune.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .models import Leg, Parlay
from .odds import format_american


@dataclass
class BuildConfig:
    target_american: float = 1500.0
    min_legs: int = 15               # preferred floor, not a hard wall
    max_legs: int = 20
    absolute_min_legs: int = 4       # below this a "parlay" isn't worth printing
    max_legs_per_game: int = 2       # >1 permits SGP fill
    thin_slate_max_per_game: int = 10  # ceiling when the slate can't fill the ticket
    prefer_cross_game: bool = True
    tolerance: float = 0.12          # acceptable |log(product/target)|
    confidence_weight: float = 1.0
    error_weight: float = 12.0
    max_iterations: int = 400

    @property
    def target_decimal(self) -> float:
        a = self.target_american
        return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


# --------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------

@dataclass
class Slot:
    """One (player, market) ladder: the rungs are alternate thresholds."""

    key: tuple[str, str]
    game_id: str
    player: str
    rungs: list[Leg]      # sorted by threshold ascending (price gets longer)
    chosen: int = 0

    @property
    def leg(self) -> Leg:
        return self.rungs[self.chosen]

    @property
    def best_confidence(self) -> float:
        return max(r.confidence for r in self.rungs)

    def reach_score(self, per_leg_log: float, error_weight: float) -> float:
        """Best confidence this ladder can offer *at a usable price*.

        Ranking slots on confidence alone fills the ticket with the surest legs,
        which are also the shortest-priced ones -- and then no amount of tuning
        can stretch the ticket to the payout target. Scoring each ladder by the
        confidence it can deliver near the required per-leg price keeps slot
        selection and price tuning pulling in the same direction.
        """
        return max(
            r.confidence - error_weight * abs(math.log(r.decimal) - per_leg_log)
            for r in self.rungs
        )


def build_slots(legs: list[Leg]) -> list[Slot]:
    grouped: dict[tuple[str, str], list[Leg]] = defaultdict(list)
    for leg in legs:
        grouped[(leg.player, leg.stat_key)].append(leg)

    slots: list[Slot] = []
    for key, rungs in grouped.items():
        rungs = sorted(rungs, key=lambda l: l.threshold)
        slots.append(
            Slot(key=key, game_id=rungs[0].game_id, player=rungs[0].player, rungs=rungs)
        )
    return slots


# --------------------------------------------------------------------------
# Stage 1: pick the slots
# --------------------------------------------------------------------------

def select_slots(
    slots: list[Slot], n_legs: int, cfg: BuildConfig, exclude_players: set[str]
) -> list[Slot] | None:
    """Greedy selection with a per-game cap and one leg per player.

    Ladders are ranked by the confidence they can deliver at the per-leg price
    the payout target implies, not by raw confidence.
    """
    per_leg_log = math.log(cfg.target_decimal) / n_legs
    ranked = sorted(
        slots, key=lambda s: -s.reach_score(per_leg_log, cfg.error_weight)
    )

    def pass_over(cap: int, chosen: list[Slot], used_players: set[str],
                  per_game: dict[str, int]) -> None:
        for slot in ranked:
            if len(chosen) >= n_legs:
                return
            if slot.player in used_players or slot.player in exclude_players:
                continue
            if per_game[slot.game_id] >= cap:
                continue
            chosen.append(slot)
            used_players.add(slot.player)
            per_game[slot.game_id] += 1

    chosen: list[Slot] = []
    used_players: set[str] = set()
    per_game: dict[str, int] = defaultdict(int)

    first_cap = 1 if cfg.prefer_cross_game else cfg.max_legs_per_game
    pass_over(first_cap, chosen, used_players, per_game)

    # Slate too thin for one-per-game: allow same-game legs up to the normal cap.
    cap = first_cap
    while len(chosen) < n_legs and cap < cfg.max_legs_per_game:
        cap += 1
        pass_over(cap, chosen, used_players, per_game)

    # Still can't field even a minimal ticket -- one game left, say. Stretch
    # past the normal cap so that yields a same-game ticket rather than
    # nothing. Deliberately gated on failing to reach absolute_min_legs: a
    # six-game slate fills 12 legs at two per game and must NOT be turned into
    # a heavy SGP just because 20 was asked for.
    if len(chosen) < cfg.absolute_min_legs:
        ceiling = max(cfg.max_legs_per_game, cfg.thin_slate_max_per_game)
        while len(chosen) < n_legs and cap < ceiling:
            cap += 1
            pass_over(cap, chosen, used_players, per_game)

    if len(chosen) < n_legs:
        return None
    return chosen


# --------------------------------------------------------------------------
# Stage 2: tune the rungs toward the target payout
# --------------------------------------------------------------------------

def _log_product(slots: list[Slot]) -> float:
    return sum(math.log(s.leg.decimal) for s in slots)


def _objective(slots: list[Slot], log_target: float, cfg: BuildConfig) -> float:
    conf = sum(s.leg.confidence for s in slots) / len(slots)
    err = abs(_log_product(slots) - log_target)
    return cfg.confidence_weight * conf - cfg.error_weight * err


def tune(slots: list[Slot], cfg: BuildConfig) -> list[Slot]:
    """Hill-climb single-rung moves until the objective stops improving."""
    log_target = math.log(cfg.target_decimal)

    # Warm start: put every slot on the rung closest to the even-split price.
    per_leg_log = log_target / len(slots)
    for slot in slots:
        slot.chosen = min(
            range(len(slot.rungs)),
            key=lambda i: abs(math.log(slot.rungs[i].decimal) - per_leg_log),
        )

    current = _objective(slots, log_target, cfg)
    for _ in range(cfg.max_iterations):
        best_move = None
        best_score = current
        for si, slot in enumerate(slots):
            original = slot.chosen
            for ri in range(len(slot.rungs)):
                if ri == original:
                    continue
                slot.chosen = ri
                score = _objective(slots, log_target, cfg)
                if score > best_score + 1e-9:
                    best_score = score
                    best_move = (si, ri)
            slot.chosen = original
        if best_move is None:
            break
        slots[best_move[0]].chosen = best_move[1]
        current = best_score

    return slots


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def build_parlay(
    legs: list[Leg],
    cfg: BuildConfig,
    slate_date: date,
    name: str = "Trend Parlay",
    n_legs: int | None = None,
    exclude_players: set[str] | None = None,
) -> Parlay | None:
    """Assemble the best ticket the slate can support.

    Leg count is a preference, not a requirement. A six-game Saturday night
    cannot produce twenty legs across twenty games, and refusing to build is
    less useful than building the best twelve-leg ticket available and saying
    so. The search walks down from the preferred count to `absolute_min_legs`
    and takes the first count the slate can actually fill; the resulting ticket
    carries notes about how it fell short.
    """
    slots = build_slots(legs)
    if not slots:
        return None

    exclude_players = exclude_players or set()
    log_target = math.log(cfg.target_decimal)

    top = n_legs or cfg.max_legs
    floor = min(cfg.absolute_min_legs, top)
    counts = list(range(top, floor - 1, -1))

    best: tuple[float, list[Slot]] | None = None
    for n in counts:
        chosen = select_slots(slots, n, cfg, exclude_players)
        if chosen is None:
            continue
        # Fresh Slot copies so leg counts don't share tuning state.
        chosen = [Slot(s.key, s.game_id, s.player, s.rungs) for s in chosen]
        tuned = tune(chosen, cfg)
        err = abs(_log_product(tuned) - log_target)
        conf = sum(s.leg.confidence for s in tuned) / len(tuned)
        score = conf - cfg.error_weight * err
        if best is None or score > best[0]:
            best = (score, tuned)
        if err <= cfg.tolerance:
            break

    if best is None:
        return None

    parlay_legs = [s.leg for s in best[1]]
    parlay_legs.sort(key=lambda l: (-l.confidence, l.est_price))
    parlay = Parlay(name=name, legs=parlay_legs, slate_date=slate_date)

    # Say plainly where the ticket fell short of what was asked for.
    wanted = n_legs or cfg.min_legs
    if len(parlay_legs) < wanted:
        n_games = len({l.game_id for l in legs})
        parlay.notes.append(
            f"Short slate: {len(parlay_legs)} legs, not {wanted} — only {n_games} "
            f"games available at {cfg.max_legs_per_game} leg(s) per game."
        )

    # Flag a ticket that leans heavily on one game. Books reprice correlated
    # legs, so the multiplied payout above is an upper bound, not a quote.
    per_game: dict[str, int] = {}
    for leg in parlay_legs:
        per_game[leg.game_id] = per_game.get(leg.game_id, 0) + 1
    biggest = max(per_game.values())
    if biggest >= 3 and biggest >= len(parlay_legs) / 2:
        label = next(l.game_label for l in parlay_legs
                     if per_game[l.game_id] == biggest)
        parlay.notes.append(
            f"{biggest} of {len(parlay_legs)} legs are in one game ({label}). "
            f"DraftKings prices this as a same-game parlay, so the real payout "
            f"will be well under the number above — check it before you stake."
        )

    shortfall = parlay.total_decimal / cfg.target_decimal
    if shortfall < 0.85:
        parlay.notes.append(
            f"Pays {format_american(parlay.total_american)}, under the "
            f"{format_american(cfg.target_american)} target — the slate ran out of "
            f"legs before the payout got there."
        )

    return parlay


def build_slate(
    legs: list[Leg],
    cfg: BuildConfig,
    slate_date: date,
    profiles: list[dict] | None = None,
) -> list[Parlay]:
    """Build several tickets that do not share players.

    Each profile may override leg count and target so one run can produce a
    heavier, higher-probability ticket alongside a longer-priced one.
    """
    profiles = profiles or [
        {"name": "Max Trend", "n_legs": cfg.max_legs},
        {"name": "Balanced", "n_legs": (cfg.min_legs + cfg.max_legs) // 2},
        {"name": "Lean", "n_legs": cfg.min_legs},
    ]

    out: list[Parlay] = []
    used: set[str] = set()
    for profile in profiles:
        sub_cfg = BuildConfig(**{**cfg.__dict__, **{
            k: v for k, v in profile.items() if k in BuildConfig.__annotations__
        }})
        parlay = build_parlay(
            legs,
            sub_cfg,
            slate_date,
            name=profile.get("name", "Trend Parlay"),
            n_legs=profile.get("n_legs"),
            exclude_players=set(used),
        )
        if parlay is None:
            continue
        out.append(parlay)
        used.update(leg.player for leg in parlay.legs)
    return out
