"""Discord webhook output."""

from __future__ import annotations

import json
import logging
from datetime import date

import requests

from .models import Parlay
from .odds import format_american

log = logging.getLogger(__name__)

SPORT_EMOJI = {"MLB": "⚾", "NFL": "🏈", "NBA": "🏀", "NHL": "🏒"}

# Our market labels are compact for the picks list; bet-slip builders parse
# natural phrasing better, so spell the combination markets out.
PLAYBOOK_MARKET = {
    "Hits+Runs+RBI": "hits + runs + RBIs",
    "Pts+Reb+Ast": "points + rebounds + assists",
    "3-Pointers Made": "three pointers made",
    "Outs Recorded": "outs recorded",
    "Shots on Goal": "shots on goal",
}


def playbook_line(parlay: "Parlay") -> str:
    """One comma-separated string of every selection, for a slip builder.

    Paste target is Playbook (playbookbot.com), which turns a list like this
    into a prefilled DraftKings/FanDuel slip. Nothing here is specific to that
    service though -- it's just the ticket in plain words, so it works with any
    slip builder that accepts typed selections.
    """
    parts = []
    for leg in parlay.legs:
        market = PLAYBOOK_MARKET.get(leg.market, leg.market.lower())
        parts.append(f"{leg.player} {leg.threshold:g}+ {market}")
    return ", ".join(parts)

COLOR_GOOD = 0x2ECC71
COLOR_WARN = 0xE67E22
COLOR_BAD = 0xE74C3C

# Discord's documented caps. A 20-leg ticket is a big embed, so messages are
# packed against the character budget rather than a fixed embed count.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_DESCRIPTION = 3600
MESSAGE_CHAR_BUDGET = 5800


def embed_chars(embed: dict) -> int:
    n = len(embed.get("title", "")) + len(embed.get("description", ""))
    for f in embed.get("fields", []):
        n += len(f.get("name", "")) + len(str(f.get("value", "")))
    n += len(embed.get("footer", {}).get("text", ""))
    return n


def chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Pack embeds into messages that fit both Discord caps."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for embed in embeds:
        size = embed_chars(embed)
        too_long = used + size > MESSAGE_CHAR_BUDGET
        too_many = len(current) >= MAX_EMBEDS_PER_MESSAGE
        if current and (too_long or too_many):
            chunks.append(current)
            current, used = [], 0
        current.append(embed)
        used += size
    if current:
        chunks.append(current)
    return chunks


def _color(parlay: Parlay) -> int:
    """Green when the ticket met its criteria as written, amber when the
    criteria had to be loosened, red when it came up short of its leg count."""
    if any("not" in n and "legs" in n for n in parlay.notes):
        return COLOR_BAD
    if parlay.notes:
        return COLOR_WARN
    return COLOR_GOOD


def _leg_lines(parlay: Parlay) -> str:
    sgp_ids = {leg.game_id for leg in parlay.sgp_legs}
    lines: list[str] = []
    for i, leg in enumerate(parlay.legs, 1):
        emoji = SPORT_EMOJI.get(leg.sport, "•")
        tag = " ⚠️SGP" if leg.game_id in sgp_ids else ""
        note = f" · {', '.join(leg.notes)}" if leg.notes else ""
        lines.append(
            f"`{i:02d}` {emoji} **{leg.player} {leg.threshold:g}+ {leg.market}** "
            f"`{leg.price_str}`{tag}\n"
            f"　　{leg.game_label} · {leg.window_record}{note}"
        )
    return "\n".join(lines)


def build_embeds(parlays: list[Parlay], slate: date, notes: list[str]) -> list[dict]:
    embeds: list[dict] = []

    # An empty slate is a normal outcome -- a late run, an off day, everything
    # already under way. Say so in plain words instead of posting a bare header
    # and leaving the reader to wonder whether the bot broke.
    if not parlays:
        return [{
            "title": "No tickets today",
            "description": (
                "Nothing on this slate could be built into a ticket. The run "
                "details below say why — most often every game has already "
                "started, or too few remain to fill the leg count.\n\n"
                "Games already under way are skipped on purpose: their pregame "
                "prices are gone and some legs are part-decided."
            ),
            "color": 0x95A5A6,
            "fields": [{
                "name": "Run details",
                "value": "\n".join(f"• {n}" for n in notes[:12]) or "No details.",
                "inline": False,
            }],
        }]

    for parlay in parlays:
        desc = _leg_lines(parlay)
        if len(desc) > MAX_DESCRIPTION:
            desc = desc[: MAX_DESCRIPTION - 6] + "\n…"

        sgp_count = len(parlay.sgp_legs)
        fields = [
            {"name": "Est. Payout", "value":
                f"**{format_american(parlay.total_american)}** "
                f"({parlay.total_decimal:.2f}x)", "inline": True},
            {"name": "Legs / Games", "value":
                f"{len(parlay.legs)} legs · {parlay.n_games} games", "inline": True},
            {"name": "$100 returns", "value":
                f"${parlay.total_decimal * 100:,.0f}", "inline": True},
            {"name": "Avg leg", "value":
                format_american(parlay.average_leg_price), "inline": True},
            {"name": "Model hit %", "value":
                f"{parlay.model_probability * 100:.1f}%", "inline": True},
            {"name": "1-in-N", "value":
                f"~1 in {1 / max(parlay.model_probability, 1e-9):,.0f}",
                "inline": True},
        ]
        if parlay.notes:
            fields.append({
                "name": "⚠️ Heads up",
                "value": "\n".join(parlay.notes)[:1000],
                "inline": False,
            })
        # Skip when a note already spells out the same-game problem in detail.
        already_warned = any("in one game" in n for n in parlay.notes)
        if sgp_count and not already_warned:
            fields.append({
                "name": "⚠️ Same-game legs",
                "value": (f"{sgp_count} legs share a game. The book will shorten "
                          f"the payout below the number above."),
                "inline": False,
            })

        embeds.append({
            "title": f"{parlay.name} · {len(parlay.legs)}-leg",
            "description": desc,
            "color": _color(parlay),
            "fields": fields,
            "footer": {"text": (
                "Prices are model estimates, not quotes — check every leg in the "
                "app before you stake. Run price_check.py with the prices you "
                "actually get to see how the ticket really priced."
            )},
        })

    if notes:
        embeds.append({
            "title": "Run notes",
            "description": "\n".join(f"• {n}" for n in notes[:15]),
            "color": 0x95A5A6,
        })

    return embeds


def slip_messages(parlays: list[Parlay]) -> list[dict]:
    """One plain message per ticket carrying nothing but the slip list.

    Discord mobile can't select text inside an embed -- which is why the old
    fenced code block was uncopyable on a phone. Long-pressing a MESSAGE does
    offer "Copy Text", so the list goes out as its own message with no heading
    and no commentary: copy it and you have exactly the list, nothing to trim.
    """
    out: list[dict] = []
    for parlay in parlays:
        out.append({
            "content": f"**{parlay.name}** — slip list is the next message:",
            "allowed_mentions": {"parse": []},
        })
        out.append({
            "content": playbook_line(parlay)[:1900],  # Discord caps content at 2000
            "allowed_mentions": {"parse": []},
        })
    return out


def send(webhook: str, parlays: list[Parlay], slate: date,
         notes: list[str] | None = None) -> bool:
    notes = notes or []
    embeds = build_embeds(parlays, slate, notes)
    if not embeds:
        return False

    header = f"**Trend Parlays — {slate.strftime('%a %b %d, %Y')}**"
    ok = True

    def post(payload: dict) -> bool:
        try:
            resp = requests.post(webhook, json=payload, timeout=20)
            if resp.status_code >= 300:
                log.error("Discord returned %s: %s", resp.status_code,
                          resp.text[:300])
                return False
        except requests.RequestException as exc:
            log.error("Discord post failed: %s", exc)
            return False
        return True

    for i, chunk in enumerate(chunk_embeds(embeds)):
        if not post({
            "content": header if i == 0 else "",
            "embeds": chunk,
            "allowed_mentions": {"parse": []},
        }):
            ok = False

    for payload in slip_messages(parlays):
        if not post(payload):
            ok = False

    return ok


def to_console(parlays: list[Parlay], slate: date, notes: list[str]) -> str:
    """Plain-text rendering for dry runs and the Actions log."""
    out = [f"=== Trend Parlays — {slate.isoformat()} ==="]
    for p in parlays:
        out.append("")
        out.append(f"--- {p.name}: {len(p.legs)} legs, {p.n_games} games ---")
        for note in p.notes:
            out.append(f"    ! {note}")
        out.append(
            f"    Est payout {format_american(p.total_american)} "
            f"({p.total_decimal:.2f}x) | avg leg "
            f"{format_american(p.average_leg_price)} | "
            f"model {p.model_probability*100:.1f}%"
        )
        for i, leg in enumerate(p.legs, 1):
            out.append(
                f"  {i:2d}. [{leg.sport}] {leg.player} {leg.threshold:g}+ "
                f"{leg.market} {leg.price_str}  ({leg.game_label}) {leg.window_record}"
            )
    if notes:
        out.append("")
        out.append("Notes:")
        out.extend(f"  - {n}" for n in notes)
    return "\n".join(out)


def write_json(parlays: list[Parlay], path: str) -> None:
    with open(path, "w") as fh:
        json.dump([p.to_dict() for p in parlays], fh, indent=2)
