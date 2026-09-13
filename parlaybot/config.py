"""Configuration: YAML file plus environment overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .builder import BuildConfig
from .calibration import CalibrationConfig
from .trends import TrendConfig


@dataclass
class Settings:
    sports: list[str] = field(default_factory=lambda: ["MLB", "NFL", "NBA", "NHL"])
    market_hold: float = 0.06
    min_minutes_to_start: int = 20
    price_band: tuple[float, float] = (-1200.0, -400.0)
    parlays: list[dict] = field(default_factory=lambda: [
        {"name": "Max Trend", "n_legs": 20},
        {"name": "Balanced", "n_legs": 18},
        {"name": "Lean", "n_legs": 15},
    ])
    trend: TrendConfig = field(default_factory=TrendConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    discord_webhook: str = ""
    discord_results_webhook: str = ""
    use_calibration: bool = True
    dry_run: bool = False
    output_dir: str = "output"
    history_dir: str = "history"

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Settings":
        raw: dict = {}
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text()) or {}

        trend = TrendConfig(**(raw.pop("trend", None) or {}))
        build = BuildConfig(**(raw.pop("build", None) or {}))
        calib = CalibrationConfig(**(raw.pop("calibration", None) or {}))

        band = raw.pop("price_band", None)
        settings = cls(
            trend=trend,
            build=build,
            calibration=calib,
            price_band=tuple(band) if band else cls.price_band,
            **{k: v for k, v in raw.items() if k in cls.__annotations__},
        )

        settings.discord_webhook = (
            os.environ.get("DISCORD_WEBHOOK_URL") or settings.discord_webhook
        )
        settings.discord_results_webhook = (
            os.environ.get("DISCORD_RESULTS_WEBHOOK_URL")
            or settings.discord_results_webhook
        )
        if os.environ.get("PARLAYBOT_DRY_RUN"):
            settings.dry_run = True
        if os.environ.get("PARLAYBOT_SPORTS"):
            settings.sports = [
                s.strip().upper()
                for s in os.environ["PARLAYBOT_SPORTS"].split(",") if s.strip()
            ]
        return settings
