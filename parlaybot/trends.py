"""Trend engine.

Turns a player's game log into candidate alternate-line legs with a modelled
probability. The design goal is the one the user stated: prefer a player with a
live success trend at a modest threshold over a nominal favourite.

The model, in order:

1.  Recency-weighted hit rate over a rolling window (exponential decay).
2.  Beta shrinkage toward a season-long prior, so 5/5 in a tiny sample does not
    read as 100%.
3.  Context adjustments in log-odds space (rest, home/away, role volatility).
4.  A trend bonus, capped, for genuinely consecutive hits.
5.  A confidence score used for ranking and for a hard filter.

Every probability that comes out of here is the model's opinion, not a market
price. `odds.estimate_price` turns it into a price to go shopping with.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import GameLogEntry, Leg, PlayerSeason
from .odds import estimate_price, inv_log_odds, log_odds


@dataclass
class TrendConfig:
    window: int = 15                 # rolling trend window (games)
    min_games: int = 8               # hard floor on sample size
    decay: float = 0.93              # per-game-back weight multiplier
    prior_strength: float = 5.0      # pseudo-games of season prior
    league_prior: float = 0.55       # fallback when no season sample exists
    max_trend_bonus: float = 0.18    # cap on the streak bonus, in log-odds
    b2b_penalty: float = 0.12        # log-odds hit for a back-to-back / short rest
    road_penalty: float = 0.04       # log-odds hit for road games
    volatility_penalty: float = 0.30 # scales the playing-time-variance penalty
    min_confidence: float = 0.45


# --------------------------------------------------------------------------
# Weighted hit rate with shrinkage
# --------------------------------------------------------------------------

def weighted_hit_rate(
    values: list[float], threshold: float, decay: float
) -> tuple[float, float, float]:
    """Return (weighted_hits, weighted_n, raw_hit_rate) for `value >= threshold`.

    `values` is newest-first, so index 0 gets full weight.
    """
    if not values:
        return 0.0, 0.0, 0.0
    w_hits = 0.0
    w_total = 0.0
    hits = 0
    for i, v in enumerate(values):
        w = decay ** i
        w_total += w
        if v >= threshold:
            w_hits += w
            hits += 1
    return w_hits, w_total, hits / len(values)


def shrink(
    w_hits: float, w_total: float, prior_prob: float, prior_strength: float
) -> float:
    """Beta-binomial posterior mean."""
    a = prior_strength * prior_prob
    b = prior_strength * (1.0 - prior_prob)
    return (w_hits + a) / (w_total + a + b)


def current_streak(values: list[float], threshold: float) -> int:
    """Consecutive games (newest-first) meeting the threshold."""
    n = 0
    for v in values:
        if v >= threshold:
            n += 1
        else:
            break
    return n


def season_rate(values: list[float], threshold: float) -> float | None:
    if not values:
        return None
    return sum(1 for v in values if v >= threshold) / len(values)


def playing_time_volatility(logs: list[GameLogEntry], limit: int) -> float:
    """Coefficient of variation of the playing-time proxy, 0 when unavailable.

    A player whose minutes/snaps swing wildly is a worse trend bet than the raw
    hit rate suggests -- the trend can evaporate the moment the role changes.
    """
    mins = [g.minutes for g in logs[:limit] if g.minutes is not None and g.minutes > 0]
    if len(mins) < 4:
        return 0.0
    mean = sum(mins) / len(mins)
    if mean <= 0:
        return 0.0
    var = sum((m - mean) ** 2 for m in mins) / len(mins)
    return math.sqrt(var) / mean


# --------------------------------------------------------------------------
# Context adjustment
# --------------------------------------------------------------------------

def adjust(
    base_prob: float,
    *,
    streak: int,
    is_home: bool,
    short_rest: bool,
    volatility: float,
    cfg: TrendConfig,
) -> tuple[float, list[str]]:
    """Apply context nudges in log-odds space so the probability stays in (0,1)."""
    x = log_odds(base_prob)
    notes: list[str] = []

    # Trend bonus: saturating in the streak length, capped.
    if streak >= 4:
        bonus = min(cfg.max_trend_bonus, 0.045 * math.log1p(streak) * math.sqrt(streak))
        x += bonus
        notes.append(f"{streak}-game streak")

    if not is_home:
        x -= cfg.road_penalty

    if short_rest:
        x -= cfg.b2b_penalty
        notes.append("short rest")

    if volatility > 0.25:
        x -= cfg.volatility_penalty * (volatility - 0.25)
        notes.append("volatile role")

    return inv_log_odds(x), notes


def confidence_score(
    *,
    n_window: int,
    streak: int,
    raw_rate: float,
    model_prob: float,
    volatility: float,
    cfg: TrendConfig,
) -> float:
    """0-1. Rewards sample size, streak length and agreement between raw and
    modelled rates; punishes role volatility."""
    sample = min(1.0, n_window / float(cfg.window))
    trend = min(1.0, streak / 8.0)
    agreement = 1.0 - min(1.0, abs(raw_rate - model_prob) * 2.5)
    stability = max(0.0, 1.0 - volatility)
    return round(
        0.35 * sample + 0.25 * trend + 0.25 * agreement + 0.15 * stability, 4
    )


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------

def candidate_thresholds(values: list[float], step: float) -> list[float]:
    """Thresholds worth testing, derived from the player's own distribution.

    Rather than hardcoding ladders per sport, walk the grid from the low end of
    the player's range up to their median. Anything above the median cannot
    plausibly price as a heavy favourite.
    """
    if not values:
        return []
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    lo = max(step, math.floor(min(ordered) / step) * step)
    hi = max(lo, math.ceil(median / step) * step)
    out: list[float] = []
    t = lo
    while t <= hi + 1e-9 and len(out) < 40:
        out.append(round(t, 2))
        t += step
    return out


def build_legs_for_player(
    player: PlayerSeason,
    *,
    markets: dict[str, dict],
    game_id: str,
    game_label: str,
    is_home: bool,
    short_rest: bool,
    cfg: TrendConfig,
    hold: float,
    price_band: tuple[float, float],
) -> list[Leg]:
    """Produce every viable leg for one player in one game.

    `markets` maps stat_key -> {"label": str, "step": float}.
    `price_band` is (min_price, max_price) in American odds, both negative,
    e.g. (-1200, -400): legs must estimate inside this band.
    """
    legs: list[Leg] = []
    volatility = playing_time_volatility(player.logs, cfg.window)

    band_lo, band_hi = min(price_band), max(price_band)

    for stat_key, meta in markets.items():
        all_vals = player.values(stat_key)
        if len(all_vals) < cfg.min_games:
            continue
        window_vals = all_vals[: cfg.window]
        if len(window_vals) < cfg.min_games:
            continue

        for threshold in candidate_thresholds(window_vals, meta["step"]):
            w_hits, w_total, raw_rate = weighted_hit_rate(
                window_vals, threshold, cfg.decay
            )
            if w_total <= 0:
                continue

            prior = season_rate(all_vals, threshold)
            prior = cfg.league_prior if prior is None else prior
            base = shrink(w_hits, w_total, prior, cfg.prior_strength)

            streak = current_streak(window_vals, threshold)
            prob, notes = adjust(
                base,
                streak=streak,
                is_home=is_home,
                short_rest=short_rest,
                volatility=volatility,
                cfg=cfg,
            )

            # A leg is only interesting if the trend is actually live.
            if raw_rate < 0.70:
                continue

            price = estimate_price(prob, hold)
            if not (band_lo <= price <= band_hi):
                continue

            conf = confidence_score(
                n_window=len(window_vals),
                streak=streak,
                raw_rate=raw_rate,
                model_prob=prob,
                volatility=volatility,
                cfg=cfg,
            )
            if conf < cfg.min_confidence:
                continue

            l15 = window_vals
            l8 = window_vals[:8]
            record = (
                f"{sum(1 for v in l8 if v >= threshold)}/{len(l8)} L8 · "
                f"{sum(1 for v in l15 if v >= threshold)}/{len(l15)} L{len(l15)} · "
                f"{sum(1 for v in all_vals if v >= threshold)}/{len(all_vals)} season"
            )

            legs.append(
                Leg(
                    sport=player.sport,
                    game_id=game_id,
                    game_label=game_label,
                    player=player.name,
                    team=player.team,
                    market=meta["label"],
                    threshold=threshold,
                    stat_key=stat_key,
                    raw_hit_rate=raw_rate,
                    model_prob=prob,
                    est_price=price,
                    n_window=len(window_vals),
                    streak=streak,
                    window_record=record,
                    confidence=conf,
                    notes=notes,
                )
            )

    return legs


def dedupe_player_market(legs: list[Leg]) -> list[Leg]:
    """Keep only the best threshold per (player, market).

    A ladder of ten thresholds for one player is one betting opinion, not ten.
    'Best' = highest confidence, tie-broken by longer price (more payout for the
    same conviction).
    """
    best: dict[tuple[str, str], Leg] = {}
    for leg in legs:
        key = (leg.player, leg.stat_key)
        cur = best.get(key)
        if cur is None or (leg.confidence, leg.est_price) > (cur.confidence, cur.est_price):
            best[key] = leg
    return list(best.values())
