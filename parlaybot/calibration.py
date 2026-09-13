"""Turn graded legs into a correction the trend model applies next time.

The method is Platt-style recalibration, kept deliberately blunt:

1.  Bucket every graded leg by the probability the model gave it.
2.  Compare the average predicted probability in a bucket to the rate that
    actually hit.
3.  Express the gap as a shift in log-odds, shrink it toward zero by sample
    size, clamp it, and store it.
4.  `trends.adjust` applies the shift for the bucket a new leg lands in.

Three guardrails, because a betting model that chases its own noise is worse
than one that never learns:

- Nothing is applied until a bucket has `min_samples` graded legs.
- The correction is shrunk by n / (n + prior_weight), so early evidence moves
  it a little and sustained evidence moves it a lot.
- It is clamped to +/- `max_shift` in log-odds, so no single bad stretch can
  swing the model wildly.

What this cannot fix: the vig. Calibration makes the probabilities honest. It
does not make a 20-leg parlay +EV, and an honest model that says "this ticket
returns 31 cents on the dollar" is the point, not a failure.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from .odds import inv_log_odds, log_odds

log = logging.getLogger(__name__)

# Buckets over model probability. Heavy favourites dominate this bot's output,
# so the top end is split finely and everything below 0.70 shares one bucket.
BUCKET_EDGES = [0.0, 0.70, 0.78, 0.83, 0.87, 0.91, 1.01]


def bucket_of(prob: float) -> int:
    for i in range(len(BUCKET_EDGES) - 1):
        if BUCKET_EDGES[i] <= prob < BUCKET_EDGES[i + 1]:
            return i
    return len(BUCKET_EDGES) - 2


def bucket_label(i: int) -> str:
    return f"{BUCKET_EDGES[i]:.0%}-{min(BUCKET_EDGES[i + 1], 1.0):.0%}"


@dataclass
class CalibrationConfig:
    min_samples: int = 60        # per bucket, before any correction applies
    prior_weight: float = 200.0  # shrinkage strength
    max_shift: float = 0.45      # clamp, in log-odds
    per_sport: bool = True


@dataclass
class Calibration:
    """Learned log-odds shifts, keyed by "SPORT:bucket" and "ALL:bucket"."""

    shifts: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    observed: dict[str, dict] = field(default_factory=dict)

    def shift_for(self, sport: str, prob: float) -> float:
        b = bucket_of(prob)
        for key in (f"{sport}:{b}", f"ALL:{b}"):
            if key in self.shifts:
                return self.shifts[key]
        return 0.0

    def apply(self, sport: str, prob: float) -> float:
        shift = self.shift_for(sport, prob)
        if shift == 0.0:
            return prob
        return inv_log_odds(log_odds(prob) + shift)

    def to_dict(self) -> dict:
        return {"shifts": self.shifts, "counts": self.counts,
                "observed": self.observed}

    @classmethod
    def empty(cls) -> "Calibration":
        return cls()


def _group(rows: list[dict], per_sport: bool) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        prob = row.get("model_prob")
        if prob is None:
            continue
        b = bucket_of(float(prob))
        groups.setdefault(f"ALL:{b}", []).append(row)
        if per_sport and row.get("sport"):
            groups.setdefault(f"{row['sport']}:{b}", []).append(row)
    return groups


def fit(rows: list[dict], cfg: CalibrationConfig | None = None) -> Calibration:
    cfg = cfg or CalibrationConfig()
    cal = Calibration()

    for key, group in _group(rows, cfg.per_sport).items():
        n = len(group)
        predicted = sum(float(r["model_prob"]) for r in group) / n
        actual = sum(1 for r in group if r.get("hit")) / n

        cal.counts[key] = n
        cal.observed[key] = {
            "n": n,
            "predicted": round(predicted, 4),
            "actual": round(actual, 4),
        }

        if n < cfg.min_samples:
            continue
        # Keep the observed rate off the 0/1 rails so log-odds stays finite.
        actual_adj = min(max(actual, 1.0 / (n + 2)), 1.0 - 1.0 / (n + 2))
        raw = log_odds(actual_adj) - log_odds(predicted)
        shrunk = raw * (n / (n + cfg.prior_weight))
        cal.shifts[key] = round(
            max(-cfg.max_shift, min(cfg.max_shift, shrunk)), 5
        )

    return cal


def load_rows(history_dir: str | Path) -> list[dict]:
    path = Path(history_dir) / "legs.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load(history_dir: str | Path) -> Calibration:
    path = Path(history_dir) / "calibration.json"
    if not path.exists():
        return Calibration.empty()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Calibration.empty()
    return Calibration(
        shifts={k: float(v) for k, v in (data.get("shifts") or {}).items()},
        counts={k: int(v) for k, v in (data.get("counts") or {}).items()},
        observed=data.get("observed") or {},
    )


def save(history_dir: str | Path, cal: Calibration) -> Path:
    d = Path(history_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "calibration.json"
    path.write_text(json.dumps(cal.to_dict(), indent=2, sort_keys=True))
    return path


def refresh(history_dir: str | Path,
            cfg: CalibrationConfig | None = None) -> Calibration:
    cal = fit(load_rows(history_dir), cfg)
    save(history_dir, cal)
    return cal


def summary_lines(cal: Calibration, limit: int = 8) -> list[str]:
    """Human-readable 'model said X, reality was Y' report."""
    rows = []
    for key, obs in cal.observed.items():
        if not key.startswith("ALL:"):
            continue
        b = int(key.split(":")[1])
        rows.append((b, obs, cal.shifts.get(key)))
    rows.sort()

    out = []
    for b, obs, shift in rows[:limit]:
        gap = obs["actual"] - obs["predicted"]
        applied = "applied" if shift else "watching"
        out.append(
            f"{bucket_label(b)}: model {obs['predicted']:.1%} vs actual "
            f"{obs['actual']:.1%} ({gap:+.1%}) over {obs['n']} legs — {applied}"
        )
    return out
