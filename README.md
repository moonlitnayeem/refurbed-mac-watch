# refurbed-mac-watch

Watches [refurbed.se](https://www.refurbed.se) on a schedule and posts the full
current results to a permanent GitHub issue after every run.

| Watch | What it looks for |
|---|---|
| `best-value-macs` | Any priced Mac with an **Apple M-series chip and at least 32 GB RAM**, ranked cheapest first |
| `mac-studio` | Any purchasable Mac Studio, with a broad **Apple Silicon Ultra** search plus direct monitoring of the [2022 M1 Ultra family](https://www.refurbed.se/p/apple-mac-studio-2022-m1-ultra/) |
| `apple-silicon-64-plus` | Any Apple-silicon Mac with **64 GB RAM or more**, including hidden RAM-selector configurations |

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

### Actionable Telegram alerts

The workflow sends Telegram messages containing the verified model, current
price, and a clickable Refurbed link for two event types:

- A Mac Studio newly appears or reappears in the `mac-studio` category. This
  existing behavior remains in place for Max and other chips. Every verified
  `M1 Ultra`, `M2 Ultra`, `M3 Ultra`, or later Apple Silicon Ultra Mac Studio
  gets the explicit Telegram title **Apple Silicon Ultra Mac Studio
  available**. Every Mac Studio search card stays excluded until its product page
  verifies chip, RAM, and SSD; Ultra cards additionally require the exact Ultra
  generation. That prevents an error response from consuming the later real
  alert. The watcher runs a broad `Apple Mac Studio Ultra` search and
  directly polls the [2022 M1 Ultra product family](https://www.refurbed.se/p/apple-mac-studio-2022-m1-ultra/),
  so that supplied family does not depend only on general-search ranking.
- An exact **MacBook Pro · M2 Max · 64 GB RAM** offer becomes purchasable below
  **23,000 kr**. “Below” is strict: 22,999 kr qualifies, while 23,000 kr does
  not. A message is sent when an offer first appears below the threshold,
  reappears there after being unavailable, or crosses down from 23,000 kr or
  higher. Remaining below the threshold—even after another price drop—does not
  repeat the same phone alert every 15 minutes. This dedicated alert is exact
  to 64 GB; the broader report still includes every Apple-silicon Mac with
  64 GB or more.

The first watcher run remains a silent baseline, and unchanged listings are not
messaged repeatedly.

Telegram setup requires no business verification, registered sender, or message
template. Create a bot with Telegram's `@BotFather`, open the bot conversation,
and press **Start** so the bot is allowed to message the account. Configure two
encrypted GitHub Actions secrets:

- `TELEGRAM_BOT_TOKEN` — the token issued by `@BotFather`
- `TELEGRAM_CHAT_ID` — the destination private-chat ID

The alert file is generated only for a real new/restock event. If an event
exists but Telegram credentials are missing or rejected, the workflow fails
before committing state so the alert can be retried rather than silently lost.
The manual **Telegram delivery test** workflow exercises the same sender and
secrets without changing watcher state or posting another report comment.

The **Best-value Macs** category is always shown first. It covers MacBook Air,
MacBook Pro, Mac mini, Mac Studio, iMac, and Mac Pro listings, but strictly
rejects Intel models, non-Mac Apple devices, machines below 32 GB RAM, and
listings without a known price. “Best value” is intentionally objective here:
the lowest current price among machines meeting those requirements gets the
highest priority, rather than relying on a speculative benchmark score.

Every current listing also shows the **previous recorded low for the same
model** with its age. For lows observed after screenshot-proof support was
enabled, the low price links to an immutable PNG under
`price-proofs/YYYY-MM-DD/`—not to the mutable Refurbed product URL. The GitHub
runner captures a 1440 × 1200 top-of-page viewport immediately when a new
all-time low is recorded, showing the product image, configuration, and price.
The cloud runner uses one Selenium-controlled headless Chrome session, clicks
**Accept all cookies**, waits for the consent dialog to disappear, and only then
captures the page. Before and after capture it inspects the rendered page and
requires an exact match for chip, RAM, SSD, and whole-kr price. A redirect, default
configuration, or changed offer is rejected before the low or screenshot can be
committed. The prior low is restored (or an unproven new key is removed), the
rest of the workflow continues, and the next run can retry. Accepted proof
records also store the PNG's SHA-256 hash.
Only new all-time lows are captured, rather than duplicating an image every 15
minutes while a price is unchanged.

“Same model” means the same Refurbed product family/generation plus chip, RAM,
and SSD capacity; keyboard, color, condition, and merchant variants are
compared together. Historical lows are backfilled from the repository's
archived reports and then maintained in `state.json`, giving a practical
target-buy price based on prices this watcher actually observed. Older lows
that predate screenshot capture remain visible as unlinked plain text; their
mutable Refurbed URLs are deliberately not presented as proof.

The report begins with a **🔔 At or Near Historical Lows Right Now** section.
A model remains there
for as long as its cheapest currently purchasable variant is at or below its
historical low, or no more than **1,000 kr above** it. Models are deduplicated by
the same hardware-comparison key used for price history, and candidates are
sorted by current price. When the qualifying listing is no longer available,
it disappears from this section automatically while its historical low remains
saved for future comparisons.

## How it works, and why it works this way

**Search only lists purchasable offers.** A Mac Studio that's out of stock has
a product page but does not appear in search results. So "a variant URL we
haven't seen before" normally covers both a brand-new listing and a restock.
The supplied 2022 M1 Ultra family is deliberately stricter: its canonical page
is polled directly on every run and qualifies only when a current product price
is present and the chip, RAM, and SSD are all parsed and verified. A failed
fetch, an unrecognized response, or a priced response without the exact verified
`M1 Ultra` generation plus RAM and SSD is treated as unknown and preserves the
last-known event state; it cannot manufacture a false restock alert or consume
the later real alert. Only Refurbed's explicit out-of-stock text records
unavailability; if search simultaneously returns zero results, unrelated saved
offers remain unknown rather than being cleared and later misreported as
restocks.

**Search cards collapse alternate configurations.** Refurbed can show one
representative variant for a whole product family while RAM, storage, color,
keyboard, and merchant configurations remain behind selectors. The 64 GB+
watch searches every Mac family, opens one representative page per product,
follows its RAM selector, verifies each RAM choice, and includes the compatible
storage/color/keyboard variants. Options inside Refurbed's “available in other
configurations” groups are never cloned as though they preserve the selected
hardware; they must be fetched and verified independently. This is how
configurations such as a 96 GB, 4 TB M2 Max become visible without assigning a
64 GB Max configuration to a cheaper 16 GB Pro URL.

**Identity is normally the variant URL, not the product name.** Result cards
link to `/p/<product-slug>/<variant-id>/`. The directly polled M1 Ultra family
uses its canonical `/p/<product-slug>/` URL when no specific purchasable variant
is returned by search; if both are present, the specific variant wins so the
report and Telegram receive only one entry. Switching between those two source
forms does not create a false availability event, while a genuinely second
variant still does. The card title is useless for filtering — it reads `Apple
MacBook Pro 2023 M3 | 16.2"` whether that's an M3 Pro with 18 GB or an M3 Max
with 64 GB.

**Config comes from the variant page `<title>`,** which is a structured string.
The canonical M1 Ultra family currently uses a shorter SEO title, so its chip,
RAM, and SSD are instead read from Refurbed's semantic `Processor`, `RAM-minne`,
and `Minne` specification rows. A structured variant title looks like:

```
Apple MacBook Pro 2023 M3 Apple M3 Max 16 Core 64.0 GB 2000 GB 16.2 " rymdsvart SE – refurbed
                          ^^^^^^^^^^^^^^^^^^^^ ^^^^^^^ ^^^^^^^
                          chip                 RAM     SSD
```

This verification isn't optional. Fuzzy searches return Intel Macs, iPads, and
Apple-silicon machines below the requested RAM. The filter therefore requires
both a Mac-family URL, a parsed M-series chip, and RAM of at least 64 GB. It is
not limited to MacBooks or exactly 64 GB: iMac, Mac mini, Mac Studio, and Mac
Pro configurations qualify too whenever Refurbed lists compatible hardware.

**The Mac Studio search is mostly noise.** It returns Mac minis, a Mac Pro, an
iMac and a Studio Display. Only results whose URL contains `apple-mac-studio`
count — the Studio Display is the trap the name-based filter would fall into.
The extra Ultra query is generation-independent, and an exact chip parser—not a
fuzzy title check—requires `M<number> Ultra` before applying the dedicated Ultra
Telegram label.

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
python3 -m unittest discover -v       # 100 tests, no network needed
gh workflow run verify-price-proof.yml # one-off cloud cookie-free capture smoke test
```

The script also runs locally on macOS with native notifications
(`--notify macos`), if you ever want that again.

## Tuning

**Check more or less often** — edit `triggers.crons` in
`cloudflare-scheduler/wrangler.jsonc`, then run `npx wrangler deploy` from that
directory. Cron Triggers use UTC.

**Change the buy-now tolerance** — edit `DEAL_PRICE_TOLERANCE_KR` in
`refurbed_watch.py`. The default is `1000`, meaning historical-low prices and
prices up to 1,000 kr above them qualify.

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
itself honestly in its `User-Agent`, leaves 2 seconds between requests, caches
verified configuration data, and backs off exponentially on errors. The 64 GB+
watch intentionally revisits representative and RAM-selector pages so newly
available hidden configurations and their current prices can be discovered.

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
