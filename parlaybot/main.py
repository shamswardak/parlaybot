"""Entry point: gather today's slate, score trends, build tickets, notify."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import discord_out
from .builder import build_slate
from .config import Settings
from .http import HttpClient
from .models import Leg
from .sources import SOURCES

log = logging.getLogger("parlaybot")


def collect_legs(settings: Settings, on: date, client: HttpClient
                 ) -> tuple[list[Leg], list[str]]:
    from . import calibration as calib
    from .trends import build_legs_for_player

    all_legs: list[Leg] = []
    notes: list[str] = []

    cal = None
    if settings.use_calibration:
        cal = calib.load(settings.history_dir)
        if cal.shifts:
            notes.append(
                f"Calibration active: {len(cal.shifts)} bucket(s) corrected from "
                f"{sum(cal.counts.values())} graded legs."
            )

    for sport in settings.sports:
        source_cls = SOURCES.get(sport)
        if source_cls is None:
            notes.append(f"{sport}: no source implemented")
            continue

        try:
            source = source_cls(client, lead_minutes=settings.min_minutes_to_start)
            matchups = source.slate(on)
        except Exception as exc:  # a dead upstream must not kill the run
            log.exception("%s slate failed", sport)
            notes.append(f"{sport}: slate unavailable ({exc.__class__.__name__})")
            continue

        if not matchups:
            # "Nothing scheduled" and "everything already started" look the
            # same from here unless the source reports what it dropped.
            dropped = getattr(source, "last_skipped", 0)
            if dropped:
                notes.append(
                    f"{sport}: {dropped} game(s) scheduled, none still bettable "
                    f"(already started or too close to first pitch)"
                )
            else:
                notes.append(f"{sport}: no games scheduled")
            continue

        try:
            pairs = source.players(matchups)
        except Exception as exc:
            log.exception("%s players failed", sport)
            notes.append(f"{sport}: player data unavailable ({exc.__class__.__name__})")
            continue

        floor = settings.min_season_games.get(sport, settings.trend.min_games)
        short_sample = sum(1 for p, _ in pairs if p.current_game_count < floor)

        sport_legs: list[Leg] = []
        for player, matchup in pairs:
            markets = (
                source.markets_for(player)
                if hasattr(source, "markets_for") else source.markets
            )
            try:
                sport_legs.extend(
                    build_legs_for_player(
                        player,
                        markets=markets,
                        game_id=matchup.game_id,
                        game_label=matchup.label,
                        is_home=source.is_home(player, matchup),
                        short_rest=source.short_rest(player, on),
                        cfg=settings.trend,
                        hold=settings.market_hold,
                        price_band=settings.price_band,
                        core_band=settings.core_price_band,
                        calibration=cal,
                        min_games=floor,
                    )
                )
            except Exception:
                log.exception("leg build failed for %s", player.name)

        if not sport_legs and short_sample == len(pairs) and pairs:
            # Early in a season this is the expected state, not a failure.
            notes.append(
                f"{sport}: {len(matchups)} games, but no player has {floor}+ games "
                f"this season yet — sitting it out until the sample is real"
            )
        else:
            note = (f"{sport}: {len(matchups)} games, {len(pairs)} players, "
                    f"{len(sport_legs)} candidate legs")
            if short_sample:
                note += f" ({short_sample} skipped for <{floor} games)"
            notes.append(note)
        all_legs.extend(sport_legs)

    return all_legs, notes


def run(settings: Settings, on: date) -> int:
    client = HttpClient()
    legs, notes = collect_legs(settings, on, client)

    if not legs:
        # An empty slate is not a failure -- it's an off day. Report it and
        # exit clean so the run doesn't show up as broken.
        log.warning("no candidate legs found for %s", on)
        if settings.discord_webhook and not settings.dry_run:
            discord_out.send(settings.discord_webhook, [], on,
                             notes + ["No qualifying legs on this slate."])
        return 0

    log.info("%d candidate legs across %d games",
             len(legs), len({l.game_id for l in legs}))

    parlays = build_slate(legs, settings.build, on, profiles=settings.parlays)
    if not parlays:
        msg = (f"Only {len({l.game_id for l in legs})} games available — not enough "
               f"for even a {settings.build.absolute_min_legs}-leg ticket.")
        notes.append(msg)
        log.warning("no parlays built: %s", msg)
        if settings.discord_webhook and not settings.dry_run:
            discord_out.send(settings.discord_webhook, [], on, notes)
        return 0

    # history/ is committed back to the repo so the grader can settle these
    # tomorrow; output/ is the throwaway copy the Actions artifact picks up.
    for directory in (settings.history_dir, settings.output_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
        discord_out.write_json(
            parlays, str(Path(directory) / f"parlays-{on.isoformat()}.json")
        )

    text = discord_out.to_console(parlays, on, notes)
    print(text)

    if settings.dry_run:
        log.info("dry run: not posting to Discord")
        return 0

    if not settings.discord_webhook:
        log.error("DISCORD_WEBHOOK_URL is not set")
        return 2

    ok = discord_out.send(settings.discord_webhook, parlays, on, notes)
    return 0 if ok else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parlaybot")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date", help="YYYY-MM-DD (defaults to today, US Eastern)")
    parser.add_argument("--tomorrow", action="store_true",
                        help="build for tomorrow's slate instead")
    parser.add_argument("--dry-run", action="store_true",
                        help="print to stdout, do not post to Discord")
    parser.add_argument("--sports", help="comma-separated override, e.g. MLB,NFL")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = Settings.load(args.config)
    if args.dry_run:
        settings.dry_run = True
    if args.sports:
        settings.sports = [s.strip().upper() for s in args.sports.split(",")]

    if args.date:
        on = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        # Sports "days" are US Eastern; the runner's clock may be UTC.
        os.environ.setdefault("TZ", "America/New_York")
        on = date.today()
        if args.tomorrow:
            on += timedelta(days=1)

    return run(settings, on)


if __name__ == "__main__":
    raise SystemExit(main())
