# refurbed-mac-watch

Watches [refurbed.se](https://www.refurbed.se) on a schedule and posts the full
current results to a permanent GitHub issue after every run.

| Watch | What it looks for |
|---|---|
| `best-value-macs` | Any priced Mac with an **Apple M-series chip and at least 32 GB RAM**, ranked cheapest first |
| `mac-studio` | Any purchasable Mac Studio |
| `mbp-max-64` | MacBook Pro with a **Max chip and 64 GB RAM** |

The watcher runs on GitHub Actions. A free Cloudflare Worker triggers it every
15 minutes because GitHub's native scheduled events can be delayed or dropped.
The watcher itself uses only the Python standard library.

## Setup

1. Create an empty repo on GitHub (private is fine).
2. Push this directory to it:

   ```bash
   git remote add origin git@github.com:<you>/refurbed-mac-watch.git
   git push -u origin main
   ```

3. **Settings → Actions → General → Workflow permissions** → select
   *Read and write permissions*. Without this the workflow cannot commit
   `state.json` or open issues.
4. Create a permanent results issue, subscribe to it, and put its number in the
   `gh issue comment` command in `.github/workflows/watch.yml`. The GitHub mobile
   app will push each new results comment when issue notifications are enabled.

The first run establishes the change-detection baseline,
but every run—including the first—posts its complete current standings.

The permanent issue keeps only its latest four report comments (about one hour
at the 15-minute schedule), so old comment cards cannot create an endless
scroll. Before an older comment is removed, its complete report is committed to
a dated file under `history/YYYY-MM/DD.md`. Every current report links to that
clickable archive. Unrelated human comments are never archived or removed.

To check it works without waiting: **Actions → refurbed watch → Run workflow**.

## Cloudflare scheduler

The deployed `refurbed-watch-scheduler` Worker runs `*/15 * * * *` and calls
GitHub's `workflow_dispatch` API. Its source and deployment configuration live
in `cloudflare-scheduler/`. GitHub's native `schedule` trigger is intentionally
disabled to prevent duplicate runs.

```bash
cd cloudflare-scheduler
npm install
npm test
npx wrangler deploy
gh auth token | npx wrangler secret put GITHUB_TOKEN
```

`GITHUB_TOKEN` is stored as an encrypted Cloudflare Worker secret and is never
committed. The current deployment is available at
`https://refurbed-watch-scheduler.moonlitnayeem.workers.dev`; it intentionally
has no public HTTP handler because it only responds to its Cron Trigger.

## What triggers an alert

- **New listing** — a variant URL that hasn't been seen before *and* passes the
  watch's filter.
- **Price drop** — a known variant is cheaper than last time.

New listings and price drops are highlighted in the report. Every run also
posts the complete current standings to the permanent results issue, even when
nothing changed. Results and multi-listing alerts are sorted from the lowest
price to the highest, with unknown prices shown last.

The **Best-value Macs** category is always shown first. It covers MacBook Air,
MacBook Pro, Mac mini, Mac Studio, iMac, and Mac Pro listings, but strictly
rejects Intel models, non-Mac Apple devices, machines below 32 GB RAM, and
listings without a known price. “Best value” is intentionally objective here:
the lowest current price among machines meeting those requirements gets the
highest priority, rather than relying on a speculative benchmark score.

## How it works, and why it works this way

**Search only lists purchasable offers.** A Mac Studio that's out of stock has
a product page but does not appear in search results. So "a variant URL we
haven't seen before" cleanly covers both a brand-new listing and a restock,
with no need to poll product pages and parse Swedish availability strings.

**Search cards collapse alternate configurations.** Refurbed can show one
representative variant for a whole product family, hiding other storage,
keyboard, and merchant configurations behind selectors. The Max/64 GB watch
therefore merges its broad search with a targeted M1 Max/64 GB/1 TB/DK search.
This exposes the requested Danish 1 TB configuration and other qualifying
alternate variants while adding only one steady-state search request per run.

**Identity is the variant URL, not the product name.** Result cards link to
`/p/<product-slug>/<variant-id>/`. The card title is useless for filtering — it
reads `Apple MacBook Pro 2023 M3 | 16.2"` whether that's an M3 Pro with 18 GB
or an M3 Max with 64 GB.

**Config comes from the variant page `<title>`,** which is a structured string:

```
Apple MacBook Pro 2023 M3 Apple M3 Max 16 Core 64.0 GB 2000 GB 16.2 " rymdsvart SE – refurbed
                          ^^^^^^^^^^^^^^^^^^^^ ^^^^^^^ ^^^^^^^
                          chip                 RAM     SSD
```

This verification isn't optional. Searching `MacBook Pro Max 64 GB` returns an
**M4 Max with 36 GB** among the results — a real false positive the filter
catches. Verified configs are cached in `state.json`, so each variant page is
fetched once, not every run.

**64 GB implies a Max chip.** On Apple Silicon MacBook Pros, Pro chips top out
at 32 GB (M1/M2 Pro), 36 GB (M3 Pro) and 48 GB (M4 Pro). Only Max chips offer
64 GB, so the RAM check does the chip filtering for free.

**The Mac Studio search is mostly noise.** It returns Mac minis, a Mac Pro, an
iMac and a Studio Display. Only results whose URL contains `apple-mac-studio`
count — the Studio Display is the trap the name-based filter would fall into.

## State, and the 60-day rule

`state.json` is committed back to the repo after each run. That does two jobs:

1. It's the memory. Without it every run would look like a first run.
2. The workflow's state commits keep the default branch active while the
   external Cloudflare scheduler triggers `workflow_dispatch`.

## Everyday use

```bash
python3 refurbed_watch.py --list      # what matches right now
python3 refurbed_watch.py --dry-run   # check without saving or notifying
python3 refurbed_watch.py --reset     # forget state, re-baseline next run
python3 -m unittest discover -v       # 47 tests, no network needed
```

The script also runs locally on macOS with native notifications
(`--notify macos`), if you ever want that again.

## Tuning

**Check more or less often** — edit `triggers.crons` in
`cloudflare-scheduler/wrangler.jsonc`, then run `npx wrangler deploy` from that
directory. Cron Triggers use UTC.

**Add a watch** — append to `WATCHES` in `refurbed_watch.py`:

```python
Watch(
    key="mini-m4",                        # stable id, used as the state key
    label="Mac mini M4 Pro",              # shown in the alert
    query="Mac mini M4 Pro",              # typed into refurbed search
    matches=lambda o: (o.ram_gb or 0) >= 48,
    needs_config=True,                    # fetch variant pages for chip/ram/ssd
),
```

`matches` receives an `Offer` with `path`, `name`, `price`, `was_price`,
`badge`, `config`, `chip`, `ram_gb`, `ssd_gb`. Set `needs_config=False` if your
filter only needs price, to skip the extra fetch.

**Only alert below a price** — add it to the filter:

```python
matches=lambda o: is_max_or_ultra_64gb(o) and (o.price or 0) < 32000,
```

## Being a good citizen

`robots.txt` on refurbed.se permits general crawling. This script identifies
itself honestly in its `User-Agent`, leaves 3 seconds between requests, caches
variant configs so it doesn't re-read pages it already knows, and backs off
exponentially on errors. Steady state is 9 search requests per run; variant
pages are cached and only fetched when a newly discovered configuration needs
verification.

## Troubleshooting

**Workflow doesn't run on schedule** — check the Cloudflare Worker's Cron
Trigger and logs, verify its `GITHUB_TOKEN` secret still exists, and confirm the
GitHub workflow is active. Manual *Run workflow* always works.

**"Permission denied" pushing state or creating an issue** — Settings → Actions
→ General → Workflow permissions → *Read and write permissions*.

**Every run reports everything as new** — `state.json` was deleted or never
committed. The next run re-baselines silently.

**"0 results (anomaly, ignored)"** — the search page returned nothing while
state held listings. Treated as a site hiccup, not a sell-out; nothing is
announced and state is preserved. If it persists, refurbed changed their
markup — run `--list` locally to see.
