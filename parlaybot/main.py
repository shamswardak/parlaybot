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

        # Teams still finishing yesterday's game: their players' logs are a
        # game behind, so any streak we quote for them may already be broken.
        try:
            stale = source.stale_teams(on)
        except Exception:
            log.exception("%s staleness check failed", sport)
            stale = set()

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
                        calibration=cal,
                        # Generate everything the outer band allows; each
                        # ticket applies its own sample rule afterwards, and
                        # the Safe ticket deliberately waives it.
                        min_games=settings.trend.min_games,
                        stale_trend=player.team in stale,
                    )
                )
            except Exception:
                log.exception("leg build failed for %s", player.name)

        # A leg built only on last season counts for the Safe ticket, where the
        # price carries the risk, but not for the trend-driven ones.
        stale_legs = sum(1 for leg in sport_legs if leg.prior_season_only)
        thin = sum(1 for leg in sport_legs if leg.current_games < floor)
        behind = sum(1 for leg in sport_legs if leg.stale_trend)
        note = (f"{sport}: {len(matchups)} games, {len(pairs)} players, "
                f"{len(sport_legs)} candidate legs")
        if behind:
            note += (f" — {behind} leg(s) from {len(stale)} team(s) still "
                     f"finishing yesterday's game, so their streaks are a game "
                     f"behind (safe ticket only)")
        if stale_legs or thin:
            note += (f" ({stale_legs} from last season only, {thin} under "
                     f"{floor} games this season — safe ticket only)")
        notes.append(note)
        all_legs.extend(sport_legs)

    return all_legs, notes


def run(settings: Settings, on: date, ticket: str | None = None,
        on_demand: bool = False) -> int:
    client = HttpClient()

    # On-demand runs post to their own channel so ad-hoc tickets don't muddle
    # the daily feed you actually placed.
    webhook = settings.discord_webhook
    if on_demand and settings.discord_ondemand_webhook:
        webhook = settings.discord_ondemand_webhook

    specs = settings.tickets
    if ticket:
        # Named tickets can come from either list: the daily one, or a ticket
        # retired from the schedule but kept for deliberate on-demand builds.
        known = settings.tickets + settings.on_demand_tickets
        specs = [s for s in known if s.name.lower() == ticket.lower()]
        if not specs:
            log.error("no ticket named %r; have: %s", ticket,
                      ", ".join(s.name for s in known))
            return 2

    legs, notes = collect_legs(settings, on, client)

    if not legs:
        # An empty slate is not a failure -- it's an off day. Report it and
        # exit clean so the run doesn't show up as broken.
        log.warning("no candidate legs found for %s", on)
        if webhook and not settings.dry_run:
            discord_out.send(webhook, [], on,
                             notes + ["No qualifying legs on this slate."])
        return 0

    log.info("%d candidate legs across %d games",
             len(legs), len({l.game_id for l in legs}))

    # Asking for one sport specifically is a decision, not an accident: honour
    # it even where that sport has too little current-season form to clear the
    # usual sample rule. The ticket still says the legs are thin.
    waive = settings.sports_overridden and len(settings.sports) == 1
    if waive:
        notes.append(
            f"{settings.sports[0]} selected on its own — the current-season "
            f"sample rule is waived for this run, so legs may rest on thin or "
            f"last-season form."
        )

    parlays = build_slate(legs, specs, on,
                          min_season_games=settings.min_season_games,
                          waive_sample=waive)
    if not parlays:
        msg = (f"Only {len({l.game_id for l in legs})} games available — not "
               f"enough legs met any ticket's criteria.")
        notes.append(msg)
        log.warning("no parlays built: %s", msg)
        if webhook and not settings.dry_run:
            discord_out.send(webhook, [], on, notes)
        return 0

    # An on-demand ticket is a different bet from the scheduled one, built at a
    # different time off a different slate. Stamping the time keeps the two
    # apart in the results channel, and the separate filename stops an ad-hoc
    # run from overwriting the ticket the grader is waiting to settle.
    suffix = ""
    if on_demand:
        stamp = datetime.now().strftime("%H:%M")
        for parlay in parlays:
            parlay.name = f"{parlay.name} (on-demand {stamp})"
        slug = (ticket or "all").lower().replace(" ", "-")
        suffix = f"-ondemand-{slug}-{datetime.now().strftime('%H%M')}"

    filename = f"parlays-{on.isoformat()}{suffix}.json"
    for directory in (settings.history_dir, settings.output_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
        discord_out.write_json(parlays, str(Path(directory) / filename))

    text = discord_out.to_console(parlays, on, notes)
    print(text)

    if settings.dry_run:
        log.info("dry run: not posting to Discord")
        return 0

    if not webhook:
        log.error("no webhook configured for this run")
        return 2

    ok = discord_out.send(webhook, parlays, on, notes)
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
    parser.add_argument("--ticket",
                        help="build only this ticket, by name (e.g. 'Safe 20')")
    parser.add_argument("--on-demand", action="store_true",
                        help="ad-hoc run: post to the on-demand channel and "
                             "save under its own history file")
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
        settings.sports_overridden = True

    if args.date:
        on = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        # Sports "days" are US Eastern; the runner's clock may be UTC.
        os.environ.setdefault("TZ", "America/New_York")
        on = date.today()
        if args.tomorrow:
            on += timedelta(days=1)

    return run(settings, on, ticket=args.ticket, on_demand=args.on_demand)


if __name__ == "__main__":
    raise SystemExit(main())
