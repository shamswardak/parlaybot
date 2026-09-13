import json
from datetime import date

from parlaybot.builder import BuildConfig, build_slate
from parlaybot.discord_out import build_embeds, chunk_embeds, embed_chars, to_console
from parlaybot.trends import TrendConfig, build_legs_for_player

from .fixtures import MARKETS, make_players, make_slate

# Discord's documented limits.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_DESCRIPTION = 4096
MAX_FIELDS = 25
MAX_TOTAL_CHARS = 6000


def _parlays():
    slate = make_slate(n_games=13)
    legs = []
    for player, matchup in make_players(slate, per_team=8):
        legs.extend(
            build_legs_for_player(
                player, markets=MARKETS, game_id=matchup.game_id,
                game_label=matchup.label,
                is_home=player.team == matchup.home_team, short_rest=False,
                cfg=TrendConfig(), hold=0.06, price_band=(-1200, -400),
            )
        )
    return build_slate(legs, BuildConfig(), date.today())


def test_embeds_respect_discord_limits():
    embeds = build_embeds(_parlays(), date.today(), ["note one", "note two"])
    assert embeds
    for e in embeds:
        assert len(e.get("description", "")) <= MAX_DESCRIPTION
        assert len(e.get("fields", [])) <= MAX_FIELDS
        assert embed_chars(e) <= MAX_TOTAL_CHARS


def test_message_chunks_stay_under_the_payload_cap():
    embeds = build_embeds(_parlays(), date.today(), ["a note"])
    chunks = chunk_embeds(embeds)
    assert sum(len(c) for c in chunks) == len(embeds)
    for chunk in chunks:
        assert len(chunk) <= MAX_EMBEDS_PER_MESSAGE
        assert sum(embed_chars(e) for e in chunk) <= MAX_TOTAL_CHARS


def test_chunker_never_drops_or_duplicates_embeds():
    fake = [{"title": f"t{i}", "description": "x" * 2500} for i in range(7)]
    chunks = chunk_embeds(fake)
    flat = [e for c in chunks for e in c]
    assert flat == fake


def test_every_leg_appears_in_its_embed():
    parlays = _parlays()
    embeds = build_embeds(parlays, date.today(), [])
    for parlay, embed in zip(parlays, embeds):
        for leg in parlay.legs:
            assert leg.player in embed["description"]


def test_payload_is_json_serialisable():
    embeds = build_embeds(_parlays(), date.today(), ["x"])
    json.dumps({"content": "hi", "embeds": embeds})


def test_console_output_lists_every_leg():
    parlays = _parlays()
    text = to_console(parlays, date.today(), [])
    for p in parlays:
        assert p.name in text
        for leg in p.legs:
            assert leg.player in text


def test_sgp_warning_present_when_games_are_doubled():
    parlays = _parlays()
    embeds = build_embeds(parlays, date.today(), [])
    for parlay, embed in zip(parlays, embeds):
        names = [f["name"] for f in embed["fields"]]
        assert ("⚠️ Same-game legs" in names) == bool(parlay.sgp_legs)
