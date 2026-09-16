#!/usr/bin/env python3
"""Re-price a generated ticket against the odds you actually found.

The daily run prints *estimated* prices. This closes the loop: paste in the real
DraftKings/FanDuel numbers and it tells you which legs the book is paying more
for than the trend justifies, which legs are worse than modelled, and what the
ticket's expected value really is.

Usage
-----
    # interactive: walks the ticket leg by leg
    python price_check.py output/parlays-2026-09-12.json

    # non-interactive: one American price per leg, in ticket order.
    # Use the --flag=value form -- negative prices look like flags otherwise.
    python price_check.py output/parlays-2026-09-12.json \\
        --ticket="Daily Ticket" --prices=-600,-540,-700,...

A leg is "value" when the posted price implies a LOWER probability than the
model's estimate -- the book is paying you more than the trend says it should.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from parlaybot.odds import (
    american_to_decimal,
    american_to_prob,
    decimal_to_american,
    format_american,
)


def load_ticket(path: Path, name: str | None) -> dict:
    tickets = json.loads(path.read_text())
    if not tickets:
        sys.exit("no tickets in that file")
    if name:
        for t in tickets:
            if t["name"].lower() == name.lower():
                return t
        sys.exit(f"no ticket named {name!r}; have: "
                 f"{', '.join(t['name'] for t in tickets)}")
    return tickets[0]


def prompt_prices(ticket: dict) -> list[float]:
    prices: list[float] = []
    print(f"\n{ticket['name']} — {ticket['n_legs']} legs. "
          f"Enter the American price you see for each leg "
          f"(blank keeps the estimate).\n")
    for i, leg in enumerate(ticket["legs"], 1):
        est = int(leg["est_price"])
        raw = input(f"  {i:2d}. {leg['selection']:<45} [{format_american(est)}]: ")
        raw = raw.strip().replace("+", "")
        prices.append(float(raw) if raw else float(est))
    return prices


def main() -> int:
    ap = argparse.ArgumentParser(
        epilog="Negative prices need the --prices=-600,-540 form, not a space."
    )
    ap.add_argument("ticket_file")
    ap.add_argument("--ticket", help="ticket name, e.g. 'Daily Ticket'")
    ap.add_argument("--prices", help="comma-separated American prices, ticket order")
    ap.add_argument("--stake", type=float, default=100.0)
    args = ap.parse_args()

    ticket = load_ticket(Path(args.ticket_file), args.ticket)
    legs = ticket["legs"]

    if args.prices:
        prices = [float(p.strip().replace("+", "")) for p in args.prices.split(",")]
        if len(prices) != len(legs):
            sys.exit(f"got {len(prices)} prices for {len(legs)} legs")
    else:
        prices = prompt_prices(ticket)

    total_est = 1.0
    total_real = 1.0
    model_p = 1.0
    rows = []

    for leg, price in zip(legs, prices):
        est_dec = american_to_decimal(leg["est_price"])
        real_dec = american_to_decimal(price)
        implied = american_to_prob(price)
        p = leg["model_prob"]
        total_est *= est_dec
        total_real *= real_dec
        model_p *= p
        edge = p - implied
        rows.append((leg, price, implied, p, edge))

    print(f"\n{'leg':<42} {'you got':>9} {'implied':>8} {'model':>7} {'edge':>8}")
    print("-" * 78)
    for leg, price, implied, p, edge in rows:
        flag = "✅" if edge > 0.01 else ("⚠️ " if edge < -0.03 else "  ")
        print(f"{leg['selection'][:41]:<42} {format_american(price):>9} "
              f"{implied*100:7.1f}% {p*100:6.1f}% {edge*100:+7.1f}% {flag}")

    ev = model_p * total_real
    print("-" * 78)
    print(f"Estimated payout : {format_american(decimal_to_american(total_est))} "
          f"({total_est:.2f}x)")
    print(f"Actual payout    : {format_american(decimal_to_american(total_real))} "
          f"({total_real:.2f}x)  -> ${args.stake * total_real:,.2f} on "
          f"${args.stake:,.0f}")
    print(f"Model hit rate   : {model_p*100:.3f}%   "
          f"(1 in {1/max(model_p,1e-12):,.0f})")
    print(f"Break-even needed: {100/total_real:.3f}%")
    print(f"Expected value   : {ev:.3f}x per $1 staked "
          f"({'+EV' if ev > 1 else 'losing proposition'})")

    positive = [r for r in rows if r[4] > 0.01]
    negative = [r for r in rows if r[4] < -0.03]
    print(f"\n{len(positive)} legs priced in your favour, "
          f"{len(negative)} priced against you.")
    if negative:
        print("Worst legs — consider swapping or dropping:")
        for leg, price, implied, p, edge in sorted(negative, key=lambda r: r[4])[:5]:
            print(f"  • {leg['selection']} at {format_american(price)} "
                  f"(model says {p*100:.1f}%, price needs {implied*100:.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
