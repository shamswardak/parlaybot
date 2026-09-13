# parlaybot

Builds 15–20 leg player-prop parlays from live success trends and posts them to
Discord every morning. You place the bets yourself in DraftKings or FanDuel.

Covers **MLB, NFL, NBA and NHL** on entirely free data — no odds subscription,
no scraping, no API keys except your Discord webhook.

---

## The honest version of what this does

Read this part before you stake anything.

**There is no live odds feed.** You chose stats-only, so the bot models each
leg's probability from game logs and *estimates* what a book would charge. Those
estimates are a shopping list, not quotes. Every price it prints must be checked
in the app.

**A 20-leg parlay at −650 a leg hits about 1 time in 30–50.** That is the
arithmetic, not pessimism: 0.85²⁰ ≈ 3.9%. The payout is ~16x. You will have long
losing stretches by design, so stake accordingly — this is a lottery-ticket
structure, not an income structure.

**Vig compounds harder than anything else in the system.** If a book holds ~6%
on each alternate prop, a 20-leg ticket returns roughly `1/1.06²⁰ ≈ 0.31` per
dollar even when every single probability estimate is perfectly correct. Leg
count, not leg quality, is what does that damage. The bot prints this as **"vig
drag"** on every ticket so the number is in front of you.

**So where can an edge actually come from?** Exactly one place: legs where the
posted price is *longer* than the trend justifies. The trend engine's job is to
find candidates; `price_check.py` is where you measure whether the price you
actually got beats the model. If it doesn't, the ticket is −EV and the bot will
tell you so.

Fewer legs is the single biggest lever you have. A 10-leg ticket at the same
per-leg prices carries half the vig drag of a 20-leg one. The config is set to
your 15–20 spec, but `build.min_legs` / `build.max_legs` are one edit away.

---

## How it picks legs

The user-facing rule you asked for — *"if Luka has had 20+ points in the last 8
games, I want that leg"* — is the starting point, not the whole model. Raw
streaks are the most over-bet signal in props, so four things are layered on top:

1. **Recency-weighted hit rate.** A rolling 15-game window with a 0.93 per-game
   decay, so the last 8 games carry ~60% of the weight. 5-for-5 recently beats
   5-for-5 a month ago.
2. **Beta shrinkage toward a season prior.** 8-for-8 in a small window does not
   get scored as 100%. The posterior mixes in ~5 pseudo-games of the player's
   season-long rate, which is what stops the bot loading tickets with small-sample
   mirages.
3. **Context adjustments**, applied in log-odds space so probabilities stay
   sane: back-to-back / short rest, home vs road, and a **role-volatility
   penalty** driven by variance in minutes, snaps or plate appearances. A player
   whose playing time swings is a worse trend bet than his hit rate suggests —
   the trend dies the moment the role changes.
4. **A capped streak bonus.** Real, but small, and it saturates. It cannot
   override the sample size.

Then it builds a **ladder of alternate thresholds** per player per market,
generated from that player's own distribution rather than hardcoded — every rung
from the low end of his range up to his median. Rungs above the median can't
price as heavy favourites, so they're dropped.

Only legs that survive all of this make the pool:

- at least 8 games of history (`trend.min_games`)
- raw hit rate ≥ 70% in the window
- estimated price inside `price_band` (default −1200 to −400, centred on your
  −650 target)
- confidence score ≥ 0.45

### Assembling the ticket

Two stages, because they pull against each other:

- **Slot selection** picks which player/market ladders go on the ticket. Crucially,
  ladders are ranked by the confidence they can deliver *at the per-leg price the
  payout target implies* — ranking by confidence alone fills the ticket with the
  surest, shortest-priced legs and then no amount of tuning can stretch it to
  +1500.
- **Rung tuning** hill-climbs which threshold each ladder sits on until the
  product of the prices converges on the target, trading as little confidence as
  possible to get there.

Constraints: one leg per player, and every distinct game is used before any game
gets a second leg. Same-game legs are only added when the slate is too thin, and
they're flagged `⚠️SGP` — the book reprices correlated legs, so the real payout
will be shorter than the printed number on those.

Three tickets a day by default (20 / 18 / 15 legs), sharing no players.

---

## Setup

### 1. Discord webhook

Server Settings → Integrations → Webhooks → **New Webhook**, point it at the
channel you want, **Copy Webhook URL**.

### 2. Push to GitHub

```bash
git init
git add .
git commit -m "parlaybot"
gh repo create parlaybot --private --source=. --push
```

Make it **private**. The repo is harmless, but the Actions log prints your
tickets.

### 3. Add the secret

Repo → Settings → Secrets and variables → Actions → **New repository secret**

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the webhook URL from step 1 |

### 4. Run it

Actions tab → **Daily parlays** → *Run workflow*. It's scheduled for 14:00 UTC
(10:00 ET) daily, which is late enough that MLB lineups and NFL inactives are
landing and early enough to shop before lines move.

The manual run accepts a sports override, a date, and a dry-run toggle.

> GitHub disables scheduled workflows on repos with no activity for 60 days.
> A commit — or a manual run — resets the clock.

### Running locally instead

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python -m parlaybot.main --dry-run          # print, don't post
python -m parlaybot.main --sports MLB,NFL   # subset
python -m parlaybot.main --date 2026-10-25  # specific slate
```

---

## Closing the loop: `price_check.py`

This is the part that turns the bot from a leg generator into something you can
evaluate. Take the ticket to DraftKings, note the prices you actually get, then:

```bash
# interactive — walks the ticket leg by leg
python price_check.py output/parlays-2026-09-12.json

# or all at once (note the = signs; negative prices look like flags otherwise)
python price_check.py output/parlays-2026-09-12.json \
    --ticket="Max Trend" --prices=-600,-540,-700,-480,...
```

It prints, per leg, what the price implies vs. what the model thinks, flags the
legs priced in your favour (✅) and against you (⚠️), and gives the ticket's real
payout, real break-even and real EV. Legs the book is charging well above the
model are the ones to swap out.

If most legs come back at ✅, the model is finding something. If they're all ⚠️,
the trends are already in the price — which is the normal outcome, and worth
knowing before you fire.

---

## Tuning

Everything lives in `config.yaml`.

| Setting | Effect |
|---|---|
| `market_hold` | Assumed book overround. Raise it → estimated prices get shorter (more conservative). 0.05–0.08 is realistic for alt props. |
| `price_band` | Which estimated prices qualify. Widen it for more legs and a looser payout fit; narrow it to concentrate on the −650 target. |
| `build.target_american` | Payout target. 1500 ≈ 16x. |
| `build.min_legs` / `max_legs` | **The vig lever.** Lower these and the drag falls sharply. |
| `build.max_legs_per_game` | Set to 1 to forbid same-game legs entirely. |
| `build.error_weight` | How hard the tuner pushes toward the payout target vs. keeping confidence. |
| `trend.window` / `decay` | How much history counts and how fast it decays. |
| `trend.prior_strength` | How hard small samples get pulled toward the season rate. Raise it if the bot is finding too many hot-streak mirages. |
| `trend.min_confidence` | Raise to 0.55+ for a much stricter pool. |

Practical note on MLB: −650 alternate lines are rarer in baseball than in
basketball. Most qualifying MLB legs will be `1+ Hits+Runs+RBI` and starting
pitcher `3+ Strikeouts` / `12+ Outs`. NBA and NHL ladders are far richer — once
those seasons start (late Oct) the bot has much more to work with.

---

## Data sources

All free, all keyless.

| Sport | Source | Notes |
|---|---|---|
| MLB | `statsapi.mlb.com` | Official, unlimited. Uses confirmed lineups when posted, top-of-order by plate appearances otherwise. |
| NFL | `nflverse-data` release CSVs | Walks a list of candidate filenames — nflverse renames assets between seasons. |
| NBA | `cdn.nba.com` static JSON | **Deliberately not `stats.nba.com`**, which IP-bans cloud providers and would break the bot on GitHub Actions. Game logs are rebuilt from box scores. |
| NHL | `api-web.nhle.com` | Official. Skaters ranked by ice time so only real roles get priced. |

Final box scores never change, so they're cached for a year and the Actions
cache makes every run after the first a small incremental fetch. A dead upstream
degrades that sport to "unavailable" in the run notes rather than killing the run.

---

## Tests

```bash
pytest -q          # if you have pytest
python run_tests.py  # dependency-free fallback, same tests
```

34 tests covering odds conversions, the shrinkage and recency behaviour, payout
targeting, cross-game constraints, SGP flagging, and Discord payload limits.

`python demo.py` runs the whole pipeline on synthetic game logs with no network
and includes a 200k-trial Monte Carlo that checks the analytic hit rate.

---

## Layout

```
parlaybot/
  odds.py          American/decimal/probability, vig, parlay math
  trends.py        recency weighting, shrinkage, context, leg generation
  builder.py       slot selection + rung tuning toward the payout target
  models.py        Leg, Parlay, PlayerSeason
  http.py          retries, throttling, disk cache
  discord_out.py   embeds, chunking, console rendering
  config.py        YAML + env
  main.py          entry point
  sources/         mlb.py nfl.py nba.py nhl.py
price_check.py     re-price a ticket against the odds you actually got
demo.py            offline end-to-end demo + Monte Carlo
run_tests.py       dependency-free test runner
```

---

Gambling involves risk. Multi-leg parlays are the highest-hold product a
sportsbook offers. Only stake money you can afford to lose, and if betting stops
being something you control, most books offer deposit limits and self-exclusion.
