"""Entry point for grading: `python -m parlaybot.grade_main`.

Grades a past slate, posts the results, appends every settled leg to the
calibration ledger, and refits the correction that the next build will use.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from . import calibration, grade, results_out
from .config import Settings
from .http import HttpClient

log = logging.getLogger("parlaybot.grade")


def run(settings: Settings, dates: list[date]) -> int:
    """Grade each slate in `dates`; report on the most recent one.

    Earlier days are re-graded silently because some upstreams publish late --
    nflverse posts Sunday's weekly stats a day or two after the games, so a
    Monday-morning grade leaves NFL legs void. Re-running the last few days
    settles them once the data lands, and the ledger's deduplication keeps
    the calibration numbers honest.
    """
    history = settings.history_dir
    client = HttpClient()

    latest: list | None = None
    latest_date: date | None = None

    for on in sorted(dates):
        tickets = grade.load_tickets(history, on)
        if tickets is None:
            continue
        results = grade.grade_tickets(tickets, on, client)
        grade.save_results(history, on, results)
        added = grade.append_graded_legs(history, on, results)
        log.info("%s: %d tickets graded, %d new legs in the ledger",
                 on, len(results), added)
        latest, latest_date = results, on

    if latest is None or latest_date is None:
        log.warning("no saved tickets to grade in %s", dates)
        return 0

    on = latest_date
    results = latest
    cal = calibration.refresh(history, settings.calibration)
    record = grade.running_record(history)

    print(results_out.to_console(results, on, record, cal))

    if settings.dry_run:
        log.info("dry run: not posting results")
        return 0

    webhook = settings.discord_results_webhook or settings.discord_webhook
    if not webhook:
        log.error("no results webhook configured")
        return 2

    ok = results_out.send(webhook, results, on, record, cal)
    return 0 if ok else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parlaybot-grade")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date", help="grade this slate only (YYYY-MM-DD)")
    parser.add_argument("--backfill", type=int, default=3,
                        help="also re-grade this many earlier days, to settle "
                             "legs whose stats published late (default 3)")
    parser.add_argument("--dry-run", action="store_true")
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

    if args.date:
        dates = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        newest = grade.previous_slate_date(settings.history_dir, date.today())
        if newest is None:
            newest = date.today() - timedelta(days=1)
        dates = [newest - timedelta(days=i)
                 for i in range(max(1, args.backfill + 1))]

    log.info("grading slates: %s", ", ".join(d.isoformat() for d in sorted(dates)))
    return run(settings, dates)


if __name__ == "__main__":
    raise SystemExit(main())
