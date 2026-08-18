# refurbed-mac-watch

Watches [refurbed.se](https://www.refurbed.se) on a schedule and opens a GitHub
issue when a machine you care about appears or drops in price.

| Watch | What it looks for |
|---|---|
| `mac-studio` | Any purchasable Mac Studio |
| `mbp-max-64` | MacBook Pro with a **Max chip and 64 GB RAM** |

Runs entirely on GitHub Actions. Nothing installed locally, no server, no
third-party service, no API keys. Standard library Python only.

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
4. Optional but recommended: create a label called `alert`, and make sure
   you're **Watching** the repo (Watch → All Activity) so new issues email you.
   The GitHub mobile app will also push them.

That's it. The first scheduled run establishes a baseline **silently** — you
start getting alerts from the second run onward.

To check it works without waiting: **Actions → refurbed watch → Run workflow**.
Run it twice; the first run baselines, the second reports.

## What triggers an alert

- **New listing** — a variant URL that hasn't been seen before *and* passes the
  watch's filter.
- **Price drop** — a known variant is cheaper than last time.

Deliberately silent on: price increases, listings disappearing, result
reordering, rating changes, and promo banners. Every run writes its full
standings to the job summary whether or not anything fired, so you can always
see current state in the Actions tab without an issue being opened.

## How it works, and why it works this way

**Search only lists purchasable offers.** A Mac Studio that's out of stock has
a product page but does not appear in search results. So "a variant URL we
haven't seen before" cleanly covers both a brand-new listing and a restock,
with no need to poll product pages and parse Swedish availability strings.

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
2. GitHub disables scheduled workflows after **60 days of no activity on the
   default branch** — and the workflow's own commits count as activity, so it
   keeps itself alive as long as listings keep changing.

If refurbed goes completely static for two months, GitHub will email you before
disabling it; re-enable from the Actions tab, or just hit *Run workflow*.

## Everyday use

```bash
python3 refurbed_watch.py --list      # what matches right now
python3 refurbed_watch.py --dry-run   # check without saving or notifying
python3 refurbed_watch.py --reset     # forget state, re-baseline next run
python3 -m unittest discover -v       # 39 tests, no network needed
```

The script also runs locally on macOS with native notifications
(`--notify macos`), if you ever want that again.

## Tuning

**Check more or less often** — edit the `cron` line in
`.github/workflows/watch.yml`. GitHub's floor is 5 minutes, and scheduled runs
can be delayed by a few minutes under load, so 15 is a sensible practical
minimum. Note that `schedule` events don't fire while a repo is disabled or
archived.

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
exponentially on errors. Steady state is 2 requests per run.

## Troubleshooting

**Workflow doesn't run on schedule** — check Actions isn't disabled for the
repo, and that the workflow hasn't been auto-disabled for inactivity. Manual
*Run workflow* always works.

**"Permission denied" pushing state or creating an issue** — Settings → Actions
→ General → Workflow permissions → *Read and write permissions*.

**Every run reports everything as new** — `state.json` was deleted or never
committed. The next run re-baselines silently.

**"0 results (anomaly, ignored)"** — the search page returned nothing while
state held listings. Treated as a site hiccup, not a sell-out; nothing is
announced and state is preserved. If it persists, refurbed changed their
markup — run `--list` locally to see.
