"""Results report for the second Discord channel."""

from __future__ import annotations

import logging
from datetime import date

import requests

from .calibration import Calibration, summary_lines
from .discord_out import MAX_DESCRIPTION, chunk_embeds
from .grade import TicketResult, format_settled_price
from .odds import format_american

log = logging.getLogger(__name__)

COLOR_WIN = 0x2ECC71
COLOR_LOSS = 0xE74C3C
COLOR_PENDING = 0x95A5A6

STATUS_EMOJI = {"WIN": "✅", "LOSS": "❌", "PENDING": "⏳"}


def _leg_lines(result: TicketResult) -> str:
    lines = []
    for r in result.legs:
        if r.status == "hit":
            mark, detail = "✅", f"{r.actual:g}"
        elif r.status == "miss":
            mark, detail = "❌", f"{r.actual:g} — needed {r.needed:g}"
        else:
            mark, detail = "⚪", "no result"
        lines.append(f"{mark} {r.label} · {detail}")
    text = "\n".join(lines)
    return text[: MAX_DESCRIPTION - 6] + "\n…" if len(text) > MAX_DESCRIPTION else text


def build_embeds(
    results: list[TicketResult],
    on: date,
    record: dict[str, int],
    cal: Calibration | None = None,
) -> list[dict]:
    embeds: list[dict] = []

    for result in results:
        color = {"WIN": COLOR_WIN, "LOSS": COLOR_LOSS}.get(
            result.status, COLOR_PENDING
        )
        fields = [
            {"name": "Result", "value":
                f"{STATUS_EMOJI[result.status]} **{result.status}**", "inline": True},
            {"name": "Legs", "value":
                f"{len(result.hits)}/{len(result.legs) - len(result.voids)} hit"
                + (f" · {len(result.voids)} void" if result.voids else ""),
                "inline": True},
            {"name": "Priced at", "value":
                format_american(result.total_american), "inline": True},
        ]

        note = result.near_miss_note()
        if note:
            fields.append({"name": "How it died", "value": note, "inline": False})
        if result.voids:
            fields.append({
                "name": "Voids",
                "value": (f"{len(result.voids)} leg(s) had no result — a book would "
                          f"drop them and settle at "
                          f"{format_settled_price(result)}."),
                "inline": False,
            })

        embeds.append({
            "title": f"{result.name} · {on.strftime('%a %b %d')}",
            "description": _leg_lines(result),
            "color": color,
            "fields": fields,
        })

    tickets = record.get("tickets", 0)
    wins = record.get("wins", 0)
    losses = record.get("losses", 0)
    hit_rate = (wins / tickets * 100) if tickets else 0.0
    summary = [
        f"**Record:** {wins}W – {losses}L"
        + (f" ({record['pending']} pending)" if record.get("pending") else ""),
        f"**Ticket hit rate:** {hit_rate:.1f}% over {tickets} tickets",
    ]

    if cal is not None:
        lines = summary_lines(cal)
        if lines:
            summary.append("")
            summary.append("**Model vs reality**")
            summary.extend(lines)
            summary.append("")
            summary.append(
                "_'applied' means enough legs have been graded that the "
                "correction is now feeding into new picks._"
            )

    embeds.append({
        "title": "Running totals",
        "description": "\n".join(summary)[:MAX_DESCRIPTION],
        "color": 0x3498DB,
    })

    return embeds


def send(webhook: str, results: list[TicketResult], on: date,
         record: dict[str, int], cal: Calibration | None = None) -> bool:
    embeds = build_embeds(results, on, record, cal)
    header = f"**Results — {on.strftime('%a %b %d, %Y')}**"
    ok = True
    for i, chunk in enumerate(chunk_embeds(embeds)):
        payload = {
            "content": header if i == 0 else "",
            "embeds": chunk,
            "allowed_mentions": {"parse": []},
        }
        try:
            resp = requests.post(webhook, json=payload, timeout=20)
            if resp.status_code >= 300:
                log.error("Discord results webhook returned %s: %s",
                          resp.status_code, resp.text[:300])
                ok = False
        except requests.RequestException as exc:
            log.error("results post failed: %s", exc)
            ok = False
    return ok


def to_console(results: list[TicketResult], on: date, record: dict[str, int],
               cal: Calibration | None = None) -> str:
    out = [f"=== Results — {on.isoformat()} ==="]
    for r in results:
        out.append("")
        out.append(f"--- {r.name}: {r.status} "
                   f"({len(r.hits)}/{len(r.legs) - len(r.voids)} legs hit) ---")
        note = r.near_miss_note()
        if note:
            out.append(f"    {note}")
        for leg in r.legs:
            mark = {"hit": "OK  ", "miss": "MISS", "void": "VOID"}[leg.status]
            out.append(f"  {mark} {leg.describe()}")
    out.append("")
    out.append(f"Record: {record.get('wins', 0)}W-{record.get('losses', 0)}L "
               f"over {record.get('tickets', 0)} tickets")
    if cal is not None:
        for line in summary_lines(cal):
            out.append(f"  {line}")
    return "\n".join(out)
