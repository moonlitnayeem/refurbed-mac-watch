#!/usr/bin/env python3
"""
refurbed_watch.py — watch refurbed.se for specific Apple hardware.

Three watches ship by default:
  * best-value-macs : cheapest Apple-silicon Macs with at least 32 GB RAM
  * mac-studio  : any purchasable Mac Studio
  * apple-silicon-64-plus : any Apple-silicon Mac with at least 64 GB RAM

Why this works: refurbed.se renders search results server-side, and its search
listing only ever contains *purchasable* offers. A model that is out of stock
simply is not there. So "a variant URL we have not seen before" is exactly the
signal we want -- it covers both a brand-new listing and a restock.

Each search result links to a variant URL of the form
    /p/<product-slug>/<variant-id>/
and that variant page's <title> is a structured config string, e.g.
    Apple MacBook Pro 2023 M3 Apple M3 Max 16 Core 64.0 GB 2000 GB 16.2 " ...
which is what we parse to confirm chip and RAM before alerting. This matters:
refurbed's search is fuzzy and will happily return an M4 Max / 36 GB machine
for the query "MacBook Pro Max 64 GB".

No third-party dependencies -- standard library only.

Usage:
    python3 refurbed_watch.py                    # normal run
    python3 refurbed_watch.py --dry-run          # check, do not save or notify
    python3 refurbed_watch.py --list             # show current matches and exit
    python3 refurbed_watch.py --reset            # forget state, re-baseline next run
    python3 refurbed_watch.py --state ./x.json   # use a specific state file
    python3 refurbed_watch.py --notify github    # force a notification backend
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import logging
import os
import platform
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

BASE = "https://www.refurbed.se"

# Be a polite client: identify honestly, and leave a gap between requests.
USER_AGENT = "refurbed-mac-watch/2.0 (personal stock alert; +https://github.com/)"
REQUEST_GAP_SECONDS = 3.0
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# Notification sound, macOS backend only. None for silence.
NOTIFY_SOUND = "Glass"

# Cap on alerts per run, so a site-wide change cannot produce a burst.
MAX_NOTIFICATIONS_PER_RUN = 8

# A current listing is a buy-now candidate when it is no more than this much
# above the all-time low observed by the watcher. Anything below the low also
# qualifies automatically.
DEAL_PRICE_TOLERANCE_KR = 1000

# Dedicated Telegram threshold requested for an exact MacBook Pro config.
M2_MAX_64GB_ALERT_THRESHOLD_KR = 23_000

# Filled by notify(); main() turns this into the GitHub issue body.
COLLECTED_ALERTS: list[dict] = []

# Actionable offers waiting for the workflow's Telegram step.
PHONE_ALERTS: list[dict] = []


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

# A search-result card links to /p/<slug>/<variant-id>/ . Plain product links
# (no variant segment) also exist on the page and must not match.
VARIANT_HREF_RE = re.compile(r'href="(/p/[a-z0-9][a-z0-9-]*/[0-9a-z]+/)"')

IMG_ALT_RE = re.compile(r'alt="([^"]*)"')

# Prices look like "33 055 kr" (current) and "35 589,35 kr" (was).
# The negative lookbehind stops the regex latching onto the trailing digit of
# the star rating, which renders immediately before the price as e.g. "4,8".
PRICE_RE = re.compile(
    r"(?<![,\d])(\d{1,3}(?:[   ]\d{3})+|\d{4,7})(?:,(\d{1,2}))?\s*kr"
)

BADGE_RE = re.compile(
    r"Bästsäljare|Bara ett fåtal kvar|Nästan slutsålda|Nyhet|Prisvärd|Toppsäljare"
)

TAG_RE = re.compile(r"<[^>]+>")
TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)

# From a variant page <title>:
#   "Apple MacBook Pro 2023 M3 Apple M3 Max 16 Core 64.0 GB 2000 GB 16.2 " ..."
CHIP_RE = re.compile(
    r"Apple\s+(M\d+)(?:\s+(Pro|Max|Ultra))?(?:\s+\d+\s+Core)?"
)
GB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*GB")


@dataclass
class Offer:
    """One purchasable configuration, identified by its variant URL."""
    path: str                      # /p/slug/variant/  -- the stable identity
    name: str = ""                 # card title, e.g. "Apple MacBook Pro 2023 M3 | 16.2"
    price: int | None = None       # current price in whole kr
    was_price: int | None = None   # struck-through reference price
    badge: str | None = None       # "Bara ett fåtal kvar" etc.
    config: str = ""               # full config string from the variant page <title>
    chip: str = ""                 # "M3 Max"
    ram_gb: float | None = None
    ssd_gb: float | None = None
    needs_verification: bool = False  # selector-derived path; fetch exact page

    @property
    def url(self) -> str:
        return BASE + self.path

    @property
    def label(self) -> str:
        """Human label preferring the verified config over the misleading card title."""
        if self.chip and self.ram_gb:
            bits = [self.chip, f"{self.ram_gb:g} GB"]
            if self.ssd_gb:
                bits.append(f"{self.ssd_gb:g} GB SSD")
            return " · ".join(bits)
        return self.config or self.name or self.path


def strip_tags(fragment: str) -> str:
    return html_mod.unescape(TAG_RE.sub(" ", fragment))


def parse_search_page(page_html: str) -> list[Offer]:
    """Pull one Offer per <article> card out of a search results page."""
    offers: list[Offer] = []
    seen: set[str] = set()

    for block in page_html.split("<article")[1:]:
        block = block.split("</article>")[0]

        href_m = VARIANT_HREF_RE.search(block)
        if not href_m:
            continue
        path = href_m.group(1)
        if path in seen:
            continue
        seen.add(path)

        text = strip_tags(block)
        text = re.sub(r"[ \t]{2,}", " ", text)

        prices = [
            (int(re.sub(r"[\s  ]", "", m.group(1))), m.group(2))
            for m in PRICE_RE.finditer(text)
        ]

        alt_m = IMG_ALT_RE.search(block)
        badge_m = BADGE_RE.search(text)

        offers.append(
            Offer(
                path=path,
                name=html_mod.unescape(alt_m.group(1)).strip() if alt_m else "",
                price=prices[0][0] if prices else None,
                was_price=prices[1][0] if len(prices) > 1 else None,
                badge=badge_m.group(0) if badge_m else None,
            )
        )

    return offers


def parse_variant_title(page_html: str) -> tuple[str, str, float | None, float | None]:
    """
    Read chip / RAM / SSD out of a variant page's <title>.

    Returns (config_string, chip, ram_gb, ssd_gb). Missing pieces come back
    empty or None rather than raising -- an unparseable page should make the
    caller cautious, not crash the run.
    """
    title_m = TITLE_RE.search(page_html)
    if not title_m:
        return "", "", None, None

    config = html_mod.unescape(title_m.group(1)).strip()
    config = re.sub(r"\s*[–-]\s*refurbed\s*$", "", config).strip()

    chip = ""
    chip_m = CHIP_RE.search(config)
    if chip_m:
        chip = f"{chip_m.group(1)} {chip_m.group(2)}".strip() if chip_m.group(2) else chip_m.group(1)

    # In the title the first "N GB" is RAM and the second is storage.
    gbs = [float(x) for x in GB_RE.findall(config)]
    ram = gbs[0] if len(gbs) >= 1 else None
    ssd = gbs[1] if len(gbs) >= 2 else None

    return config, chip, ram, ssd


def parse_ram_variant_paths(page_html: str) -> list[str]:
    """Return variant paths exposed by a product page's RAM selector."""
    paths: list[str] = []
    seen: set[str] = set()
    for select in re.findall(r"<select\b[^>]*>.*?</select>", page_html,
                             flags=re.IGNORECASE | re.DOTALL):
        # RAM choices use decimal values (for example 64.0 GB), while the
        # storage selector uses whole values such as 1000 GB.
        if not re.search(r">\s*\d+(?:\.\d+)?\s*GB\s*</option>", select,
                         flags=re.IGNORECASE):
            continue
        if not re.search(r">\s*\d+\.\d+\s*GB\s*</option>", select,
                         flags=re.IGNORECASE):
            continue
        for value in re.findall(r'value="(/p/[^"]+)"', select, flags=re.IGNORECASE):
            path = urllib.parse.urlsplit(value).path
            if VARIANT_HREF_RE.fullmatch(f'href="{path}"') and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def parse_product_price(page_html: str) -> int | None:
    """Read the currently selected offer price from a product page."""
    match = re.search(
        r'data-test="product-price"[^>]*>(.{0,300})',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    price = PRICE_RE.search(strip_tags(match.group(1)))
    if not price:
        return None
    return int(re.sub(r"[\s  ]", "", price.group(1)))


def parse_configuration_variant_offers(page_html: str, selected: Offer) -> list[Offer]:
    """Collect same-config selector paths for exact page verification."""
    variants: list[Offer] = []
    seen: set[str] = set()
    for select in re.findall(r"<select\b[^>]*>.*?</select>", page_html,
                             flags=re.IGNORECASE | re.DOTALL):
        # RAM options may switch the processor too, so those are fetched and
        # verified separately rather than cloned from the selected config.
        if re.search(r">\s*\d+\.\d+\s*GB\s*</option>", select,
                     flags=re.IGNORECASE):
            continue

        # Refurbed nests options that change multiple hardware attributes under
        # “available in other configurations”. They are not siblings of the
        # selected hardware and must be fetched/verified separately.
        same_configuration = re.sub(
            r"<optgroup\b[^>]*>.*?</optgroup>",
            "",
            select,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for attrs, _option_html in re.findall(
            r"<option\b([^>]*)>(.*?)</option>", same_configuration,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            if re.search(r'data-(?:type="optgroup-option"|optgroup-label=)',
                         attrs, flags=re.IGNORECASE):
                continue
            value_m = re.search(r'value="(/p/[^"]+)"', attrs, flags=re.IGNORECASE)
            if not value_m:
                continue
            path = urllib.parse.urlsplit(value_m.group(1)).path
            if not VARIANT_HREF_RE.fullmatch(f'href="{path}"') or path in seen:
                continue
            seen.add(path)
            # Even non-optgroup selectors can expose stale price deltas. Never
            # infer hardware or price from the selected page; the destination
            # page must provide both before this candidate can match.
            variants.append(Offer(path=path, needs_verification=True))
    return variants


# --------------------------------------------------------------------------
# Watches
# --------------------------------------------------------------------------

@dataclass
class Watch:
    key: str
    label: str
    query: str
    # Returns True if this offer is one the user actually wants to hear about.
    matches: Callable[[Offer], bool] = lambda o: True
    # If True, fetch the variant page to fill in chip/RAM before deciding.
    needs_config: bool = False
    # Refurbed collapses alternate configurations into one search card. Extra
    # targeted searches expose variants hidden behind the card's selectors.
    extra_queries: tuple[str, ...] = ()
    # If True, inspect one representative page per product family and follow
    # the RAM selector to variants hidden from search-result cards.
    discover_ram_variants: bool = False
    # A broad watch may need the product family in addition to the config.
    offer_label: Callable[[Offer], str] = lambda o: o.label


def is_mac_studio(o: Offer) -> bool:
    """
    The "Mac Studio" search is fuzzy and returns Mac minis, iMacs, a Mac Pro
    and a Studio Display. Only the URL slug is trustworthy.
    """
    return "apple-mac-studio" in o.path


def is_max_or_ultra_64gb(o: Offer) -> bool:
    """MacBook Pro with a Max (or Ultra, should one ever ship) chip and 64 GB."""
    if o.ram_gb is None or abs(o.ram_gb - 64.0) > 0.01:
        return False
    return bool(re.search(r"\b(Max|Ultra)\b", o.chip))


def is_best_value_mac(o: Offer) -> bool:
    """A priced Mac with Apple silicon and at least 32 GB RAM."""
    return bool(is_apple_silicon_mac(o, min_ram_gb=32) and o.price)


def is_apple_silicon_mac(o: Offer, min_ram_gb: float) -> bool:
    """A Mac-family product with an M-series chip and the requested RAM."""
    mac_slugs = (
        "/p/apple-macbook-",
        "/p/apple-mac-mini-",
        "/p/apple-mac-studio-",
        "/p/apple-imac-",
        "/p/apple-mac-pro-",
    )
    is_mac = o.path.startswith(mac_slugs)
    is_apple_silicon = bool(re.fullmatch(r"M[1-9]\d*(?: (?:Pro|Max|Ultra))?", o.chip))
    return bool(is_mac and is_apple_silicon and (o.ram_gb or 0) >= min_ram_gb)


def is_apple_silicon_64gb_or_more(o: Offer) -> bool:
    """Any Apple-silicon Mac with at least 64 GB RAM."""
    return is_apple_silicon_mac(o, min_ram_gb=64)


def is_m2_max_64gb_under_threshold(o: Offer) -> bool:
    """Exact 64 GB M2 Max MacBook Pro priced strictly below the alert level."""
    return bool(
        "/p/apple-macbook-pro-" in o.path
        and o.chip == "M2 Max"
        and o.ram_gb is not None
        and abs(o.ram_gb - 64.0) <= 0.01
        and o.price is not None
        and o.price < M2_MAX_64GB_ALERT_THRESHOLD_KR
    )


def best_value_offer_label(o: Offer) -> str:
    """Identify the Mac family as well as its hardware configuration."""
    families = (
        ("apple-macbook-pro-", "MacBook Pro"),
        ("apple-macbook-air-", "MacBook Air"),
        ("apple-mac-mini-", "Mac mini"),
        ("apple-mac-studio-", "Mac Studio"),
        ("apple-imac-", "iMac"),
        ("apple-mac-pro-", "Mac Pro"),
    )
    family = next((name for slug, name in families if slug in o.path), "Mac")
    return f"{family} · {o.label}"


WATCHES: list[Watch] = [
    Watch(
        key="best-value-macs",
        label="Best-value Macs · Apple silicon · 32 GB+",
        query="Apple Mac 32 GB",
        matches=is_best_value_mac,
        needs_config=True,
        offer_label=best_value_offer_label,
        extra_queries=(
            "Apple MacBook 32 GB",
            "Apple Mac mini 32 GB",
            "Apple Mac Studio 32 GB",
            "Apple iMac 32 GB",
            "Apple Mac Pro 32 GB",
        ),
    ),
    Watch(
        key="mac-studio",
        label="Mac Studio",
        query="Mac Studio",
        matches=is_mac_studio,
        needs_config=True,   # enriches the alert text; filter is URL-based
    ),
    Watch(
        key="apple-silicon-64-plus",
        label="Apple silicon Macs · 64 GB+",
        query="Apple Mac 64 GB",
        matches=is_apple_silicon_64gb_or_more,
        needs_config=True,
        discover_ram_variants=True,
        offer_label=best_value_offer_label,
        extra_queries=(
            "Apple MacBook 64 GB",
            "Apple Mac mini 64 GB",
            "Apple Mac Studio 64 GB",
            "Apple iMac 64 GB",
            "Apple Mac Pro 64 GB",
        ),
    ),
]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_last_request_at = 0.0


def fetch(url: str) -> str | None:
    """GET a URL with retries and a polite inter-request gap. None on failure."""
    global _last_request_at

    for attempt in range(1, MAX_RETRIES + 1):
        gap = REQUEST_GAP_SECONDS - (time.time() - _last_request_at)
        if gap > 0:
            time.sleep(gap)

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            _last_request_at = time.time()
            return body
        except urllib.error.HTTPError as e:
            _last_request_at = time.time()
            if e.code == 404:
                logging.warning("404 for %s", url)
                return None
            logging.warning("HTTP %s for %s (attempt %d/%d)", e.code, url, attempt, MAX_RETRIES)
        except Exception as e:  # noqa: BLE001 - never let a network blip kill the run
            _last_request_at = time.time()
            logging.warning("fetch failed for %s: %s (attempt %d/%d)", url, e, attempt, MAX_RETRIES)

        if attempt < MAX_RETRIES:
            time.sleep(min(2 ** attempt, 8) + random.random())

    return None


def search_url(query: str) -> str:
    return f"{BASE}/search/?query=" + urllib.parse.quote_plus(query)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

NOTIFY_BACKEND = "auto"


def resolve_backend() -> str:
    if NOTIFY_BACKEND != "auto":
        return NOTIFY_BACKEND
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github"
    if platform.system() == "Darwin":
        return "macos"
    return "none"


def notify(title: str, subtitle: str, message: str, url: str | None = None) -> None:
    """
    Single sink for every alert.

    Always records the alert so main() can build a report from it; additionally
    delivers it natively on macOS. Under GitHub Actions the recording is the
    delivery -- the workflow turns COLLECTED_ALERTS into an issue.
    """
    COLLECTED_ALERTS.append(
        {"title": title, "subtitle": subtitle, "message": message, "url": url}
    )

    if resolve_backend() != "macos":
        return

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(message)}" '
        f'with title "{esc(title)}" '
        f'subtitle "{esc(subtitle)}"'
    )
    if NOTIFY_SOUND:
        script += f' sound name "{esc(NOTIFY_SOUND)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=15,
                       capture_output=True)
    except Exception as e:  # noqa: BLE001
        logging.warning("notification failed: %s", e)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 2, "watches": {}}
    try:
        with path.open(encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("watches", {})
        return state
    except Exception as e:  # noqa: BLE001 - corrupt state must not wedge the watcher
        logging.warning("state unreadable (%s); starting fresh", e)
        try:
            path.replace(path.with_suffix(".corrupt.json"))
        except Exception:  # noqa: BLE001
            pass
        return {"version": 2, "watches": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    tmp.replace(path)  # atomic: never leave a half-written state file


def fmt_kr(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n:,}".replace(",", " ") + " kr"


def price_history_key(offer: Offer) -> str | None:
    """Group variants that share model, chip, RAM, and storage."""
    if not offer.chip or offer.ram_gb is None or offer.ssd_gb is None:
        return None
    parts = offer.path.strip("/").split("/")
    if len(parts) < 3:
        return None
    slug = parts[1]
    return f"{slug}|{offer.chip}|{offer.ram_gb:g}|{offer.ssd_gb:g}"


def parse_archived_price_lows(markdown: str) -> dict[str, dict]:
    """Extract all-time model lows from archived issue-report Markdown."""
    lows: dict[str, dict] = {}
    seen_at = ""
    item_re = re.compile(
        r"\[([^\]]+)\]\((https://www\.refurbed\.se(/p/[^)]+))\)\s+—\s+"
        r"(\d[\d\s  ]*)\s*kr"
    )
    for line in markdown.splitlines():
        summary = re.search(r"<summary>Update — ([^<]+)</summary>", line)
        if summary:
            try:
                parsed = datetime.strptime(summary.group(1), "%Y-%m-%d %H:%M UTC")
                seen_at = parsed.strftime("%Y-%m-%dT%H:%M:00Z")
            except ValueError:
                seen_at = ""
            continue

        item = item_re.search(line)
        if not item or not seen_at:
            continue
        label, url, path, price_text = item.groups()
        chip_m = re.search(r"\b(M\d+(?: (?:Pro|Max|Ultra))?)\b", label)
        ram_m = re.search(r"·\s*(\d+(?:\.\d+)?)\s*GB(?:\s*·|\s*$)", label)
        ssd_m = re.search(r"·\s*(\d+(?:\.\d+)?)\s*GB SSD", label)
        if not (chip_m and ram_m and ssd_m):
            continue
        offer = Offer(
            path=urllib.parse.urlsplit(path).path,
            chip=chip_m.group(1),
            ram_gb=float(ram_m.group(1)),
            ssd_gb=float(ssd_m.group(1)),
        )
        key = price_history_key(offer)
        if key is None:
            continue
        price = int(re.sub(r"[\s  ]", "", price_text))
        previous = lows.get(key)
        if previous is None or price < previous["price"]:
            lows[key] = {"price": price, "url": url, "seen_at": seen_at}
    return lows


def load_archived_price_lows(history_dir: Path = Path("history")) -> dict[str, dict]:
    lows: dict[str, dict] = {}
    if not history_dir.exists():
        return lows
    for path in sorted(history_dir.rglob("*.md")):
        for key, record in parse_archived_price_lows(
            path.read_text(encoding="utf-8")
        ).items():
            previous = lows.get(key)
            if previous is None or record["price"] < previous["price"]:
                lows[key] = record
    return lows


def historical_low_markdown(record: dict) -> str:
    """Render a low price linked only to its immutable screenshot proof."""
    price = fmt_kr(record.get("price"))
    screenshot = record.get("screenshot")
    validation = record.get("proof_validation")
    if (not isinstance(screenshot, str)
            or not screenshot.startswith("price-proofs/")
            or not isinstance(validation, dict)
            or validation.get("version") != 1):
        return price
    repo = os.environ.get("GITHUB_REPOSITORY", "moonlitnayeem/refurbed-mac-watch")
    proof_path = urllib.parse.quote(screenshot, safe="/")
    return f"[{price}](https://github.com/{repo}/blob/main/{proof_path})"


def previous_low_suffix(offer: Offer, state: dict) -> str:
    key = price_history_key(offer)
    record = state.get("price_lows", {}).get(key) if key else None
    if not record:
        return ""
    try:
        seen = datetime.strptime(record["seen_at"], "%Y-%m-%dT%H:%M:%SZ")
        age_days = max(0, int((time.time() - seen.replace(tzinfo=timezone.utc).timestamp()) // 86400))
        age = "today" if age_days == 0 else f"{age_days} day{'s' if age_days != 1 else ''} ago"
    except (KeyError, TypeError, ValueError):
        age = "previously"
    return f" · Previous low for same model: {historical_low_markdown(record)} ({age})"


def update_price_lows(state: dict, seen_at: str) -> list[dict]:
    """Update permanent lows and return screenshot requests for new records."""
    lows = dict(state.get("price_lows", {}))
    screenshot_requests: list[dict] = []
    cheapest_by_model: dict[str, Offer] = {}

    for watch in state.get("watches", {}).values():
        for path, data in watch.get("offers", {}).items():
            if not data.get("matched") or data.get("price") is None:
                continue
            offer = Offer(
                path=path,
                price=data["price"],
                chip=data.get("chip", ""),
                ram_gb=data.get("ram_gb"),
                ssd_gb=data.get("ssd_gb"),
            )
            assert offer.price is not None
            key = price_history_key(offer)
            if key is None:
                continue
            current = cheapest_by_model.get(key)
            if current is None or current.price is None or offer.price < current.price:
                cheapest_by_model[key] = offer

    for key, offer in cheapest_by_model.items():
        previous = lows.get(key)
        if previous is None or offer.price < previous["price"]:
            lows[key] = {"price": offer.price, "url": offer.url, "seen_at": seen_at}
            date = seen_at[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", seen_at) else "unknown-date"
            digest = hashlib.sha256(
                f"{key}|{offer.price}|{seen_at}".encode("utf-8")
            ).hexdigest()[:16]
            screenshot_requests.append({
                "key": key,
                "price": offer.price,
                "url": offer.url,
                "seen_at": seen_at,
                "previous": previous,
                "screenshot": f"price-proofs/{date}/{offer.price}kr-{digest}.png",
            })
    state["price_lows"] = lows
    return screenshot_requests


def write_price_proof_requests(
    requests: list[dict], path: Path = Path("price_proof_requests.json")
) -> None:
    """Write new-low screenshot work for the browser step, removing stale work."""
    if not requests:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(requests, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_buy_now_lines(state: dict, tolerance_kr: int = DEAL_PRICE_TOLERANCE_KR,
                        active_watch_keys: list[str] | None = None) -> list[str]:
    """Build the top section for currently available near-record-low Macs."""
    cheapest_by_model: dict[str, Offer] = {}
    active = set(active_watch_keys) if active_watch_keys is not None else None
    for watch_key, watch in state.get("watches", {}).items():
        if active is not None and watch_key not in active:
            continue
        for path, data in watch.get("offers", {}).items():
            price = data.get("price")
            if not data.get("matched") or not isinstance(price, int):
                continue
            offer = Offer(
                path=path,
                price=price,
                chip=data.get("chip", ""),
                ram_gb=data.get("ram_gb"),
                ssd_gb=data.get("ssd_gb"),
            )
            key = price_history_key(offer)
            if key is None or key not in state.get("price_lows", {}):
                continue
            current = cheapest_by_model.get(key)
            if current is None or current.price is None or offer.price < current.price:
                cheapest_by_model[key] = offer

    candidates: list[tuple[Offer, dict]] = []
    for key, offer in cheapest_by_model.items():
        record = state["price_lows"][key]
        if offer.price <= record["price"] + tolerance_kr:
            candidates.append((offer, record))
    candidates.sort(key=lambda item: (item[0].price is None, item[0].price or 0))

    title = (
        f"🔔 At or Near Historical Lows Right Now · within {fmt_kr(tolerance_kr)} "
        f"of historical low ({len(candidates)})"
    )
    lines = [f"__STANDINGS__{title}"]
    if not candidates:
        lines.append(
            f"__ITEM___none currently available within {fmt_kr(tolerance_kr)} "
            "of its historical low_"
        )
        return lines

    for offer, record in candidates:
        difference = offer.price - record["price"]
        if difference < 0:
            comparison = f"{fmt_kr(abs(difference))} below historical low"
        elif difference == 0:
            comparison = "matches historical low"
        else:
            comparison = f"{fmt_kr(difference)} above historical low"
        lines.append(
            f"__ITEM__[{best_value_offer_label(offer)}]({offer.url}) — "
            f"{fmt_kr(offer.price)} · Historical low: "
            f"{historical_low_markdown(record)} · {comparison}"
        )
    return lines


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

def enrich_from_page(offer: Offer, page: str) -> None:
    """Fill configuration and selected-offer price from a product page."""
    offer.config, offer.chip, offer.ram_gb, offer.ssd_gb = parse_variant_title(page)
    product_price = parse_product_price(page)
    if product_price is not None:
        offer.price = product_price


def enrich(offer: Offer) -> None:
    """Fill in chip / RAM / SSD from the variant page."""
    page = fetch(offer.url)
    if page is not None:
        enrich_from_page(offer, page)


def discover_hidden_ram_variants(offers_by_path: dict[str, Offer]) -> None:
    """Follow RAM dropdowns from one search result per product family."""
    representatives: dict[str, Offer] = {}
    for offer in offers_by_path.values():
        parts = offer.path.strip("/").split("/")
        if len(parts) >= 3:
            representatives.setdefault(parts[1], offer)

    pages: dict[str, str] = {}
    hidden_paths: set[str] = set()

    def add_sibling_configurations(offer: Offer, page: str) -> None:
        if (offer.ram_gb or 0) < 64:
            return
        for variant in parse_configuration_variant_offers(page, offer):
            existing = offers_by_path.get(variant.path)
            if existing is None or not existing.config:
                offers_by_path[variant.path] = variant

    for offer in representatives.values():
        page = fetch(offer.url)
        if page is None:
            continue
        pages[offer.path] = page
        enrich_from_page(offer, page)
        hidden_paths.update(parse_ram_variant_paths(page))
        add_sibling_configurations(offer, page)

    for path in sorted(hidden_paths):
        offer = offers_by_path.setdefault(path, Offer(path=path))
        page = pages.get(path)
        if page is None:
            page = fetch(offer.url)
        if page is not None:
            enrich_from_page(offer, page)
            add_sibling_configurations(offer, page)


def run_watch(watch: Watch, state: dict, dry_run: bool,
              notifications_left: list[int]) -> list[str]:
    """Check one watch. Returns human-readable lines describing what happened."""
    lines: list[str] = []
    offers_by_path: dict[str, Offer] = {}
    for index, query in enumerate((watch.query, *watch.extra_queries)):
        page = fetch(search_url(query))
        if page is None:
            if index == 0:
                logging.error("[%s] search fetch failed; skipping this run", watch.key)
                return [f"{watch.label}: fetch failed, skipped"]
            logging.warning("[%s] extra search fetch failed for %r; continuing",
                            watch.key, query)
            continue

        query_offers = parse_search_page(page)
        logging.info("[%s] %d cards for query %r", watch.key, len(query_offers), query)
        for offer in query_offers:
            offers_by_path.setdefault(offer.path, offer)

    offers = list(offers_by_path.values())

    if watch.discover_ram_variants:
        discover_hidden_ram_variants(offers_by_path)
        offers = list(offers_by_path.values())

    prev = state["watches"].get(watch.key, {})
    prev_offers: dict = prev.get("offers", {})
    is_first_run = not prev_offers

    # A zero-result page almost always means the site hiccuped or changed its
    # markup, not that every listing vanished. Refuse to diff against it.
    if not offers and prev_offers:
        logging.error("[%s] 0 results but %d known; treating as anomaly, not a sell-out",
                      watch.key, len(prev_offers))
        return [f"{watch.label}: 0 results (anomaly, ignored)"]

    # Reuse cached config for variants we already verified; only pay for new ones.
    matched: dict[str, Offer] = {}
    for o in offers:
        cached = prev_offers.get(o.path)
        if o.needs_verification:
            enrich(o)
        elif o.config:
            pass
        elif cached and cached.get("config"):
            o.config = cached.get("config", "")
            o.chip = cached.get("chip", "")
            o.ram_gb = cached.get("ram_gb")
            o.ssd_gb = cached.get("ssd_gb")
            if o.price is None:
                o.price = cached.get("price")
        elif watch.needs_config:
            enrich(o)

        if watch.matches(o):
            matched[o.path] = o
        else:
            logging.info("[%s] filtered out %s (%s, %s GB)",
                         watch.key, o.path, o.chip or "?", o.ram_gb)

    events: list[tuple[str, Offer, int | None]] = []
    for path, o in matched.items():
        old = prev_offers.get(path)
        if old is None or not old.get("matched"):
            events.append(("NEW", o, None))
        else:
            old_price = old.get("price")
            if old_price and o.price and o.price < old_price:
                events.append(("DROP", o, old_price))

    # First run establishes the baseline. Announcing all of it would be noise.
    if is_first_run:
        logging.info("[%s] first run: baselining %d matches, no alerts",
                     watch.key, len(matched))
        events = []
        lines.append(f"{watch.label}: baseline set ({len(matched)} matches)")
    elif not events:
        cheapest = min((o for o in matched.values() if o.price),
                       key=lambda x: x.price, default=None)
        tail = f", cheapest {fmt_kr(cheapest.price)}" if cheapest else ""
        lines.append(f"{watch.label}: no change ({len(matched)} matches{tail})")

    if (watch.key == "apple-silicon-64-plus" and not is_first_run
            and not dry_run):
        for path, o in matched.items():
            if not is_m2_max_64gb_under_threshold(o):
                continue
            old = prev_offers.get(path)
            old_qualified = bool(
                old
                and old.get("matched")
                and old.get("chip") == "M2 Max"
                and old.get("ram_gb") is not None
                and abs(old["ram_gb"] - 64.0) <= 0.01
                and old.get("price") is not None
                and old["price"] < M2_MAX_64GB_ALERT_THRESHOLD_KR
            )
            if not old_qualified:
                PHONE_ALERTS.append({
                    "title": "M2 Max 64 GB below 23 000 kr",
                    "model": best_value_offer_label(o),
                    "price": o.price,
                    "url": o.url,
                })

    # Keep alerts and the issue report consistent with the standings: cheapest
    # first, unknown prices last.
    events.sort(key=lambda event: (event[1].price is None, event[1].price or 0))

    for kind, o, old_price in events:
        offer_label = watch.offer_label(o)
        if kind == "NEW":
            title = f"New: {watch.label}"
            message = fmt_kr(o.price) + (f" · {o.badge}" if o.badge else "")
            line = f"NEW  {offer_label} — {fmt_kr(o.price)} — {o.url}"
        else:
            title = f"Price drop: {watch.label}"
            message = f"{fmt_kr(old_price)} → {fmt_kr(o.price)}"
            line = f"DROP {offer_label} — {fmt_kr(old_price)} → {fmt_kr(o.price)} — {o.url}"

        lines.append(line)
        logging.info("[%s] %s", watch.key, line)

        if kind == "NEW" and watch.key == "mac-studio" and not dry_run:
            PHONE_ALERTS.append({
                "model": o.label,
                "price": o.price,
                "url": o.url,
            })

        if not dry_run and notifications_left[0] > 0:
            notify(title, offer_label, message, url=o.url)
            notifications_left[0] -= 1

    # Persist every offer we saw, matched or not, so rejected variants are not
    # re-fetched on every single run.
    new_offers = {
        o.path: {
            "name": o.name,
            "price": o.price,
            "was_price": o.was_price,
            "badge": o.badge,
            "config": o.config,
            "chip": o.chip,
            "ram_gb": o.ram_gb,
            "ssd_gb": o.ssd_gb,
            "matched": o.path in matched,
        }
        for o in offers
    }

    if not dry_run:
        state["watches"][watch.key] = {
            "label": watch.label,
            "query": watch.query,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "offers": new_offers,
        }

    # Standings, always, cheapest first.
    if matched:
        lines.append(f"__STANDINGS__{watch.label} ({len(matched)})")
        for o in sorted(matched.values(), key=lambda x: (x.price is None, x.price or 0)):
            lines.append(f"__ITEM__[{watch.offer_label(o)}]({o.url}) — {fmt_kr(o.price)}"
                         + (f" · {o.badge}" if o.badge else "")
                         + previous_low_suffix(o, state))
    else:
        lines.append(f"__STANDINGS__{watch.label} (0)")
        lines.append("__ITEM___none currently listed_")

    return lines


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def build_report(all_lines: list[str]) -> tuple[str, str]:
    """
    Split the collected lines into (alert_markdown, standings_markdown).

    Lines tagged __STANDINGS__ / __ITEM__ form the always-printed standings;
    everything else is status or event text.
    """
    standings: list[str] = []
    status: list[str] = []
    for line in all_lines:
        if line.startswith("__STANDINGS__"):
            standings.append(f"\n**{line[len('__STANDINGS__'):]}**\n")
        elif line.startswith("__ITEM__"):
            standings.append(f"- {line[len('__ITEM__'):]}")
        else:
            status.append(line)

    alert_md = ""
    if COLLECTED_ALERTS:
        parts = []
        for a in COLLECTED_ALERTS:
            link = f"[{a['subtitle']}]({a['url']})" if a.get("url") else a["subtitle"]
            kind = "PRICE DROP" if a["title"].startswith("Price drop") else "NEW"
            parts.append(f"- **{kind}** — {link} — {a['message']}")
        alert_md = "\n".join(parts)

    return alert_md, "\n".join(standings).strip()


def write_github_outputs(alert_md: str, standings_md: str, status: list[str]) -> None:
    """Expose results to the workflow via GITHUB_OUTPUT and an issue body file."""
    has_alerts = bool(alert_md)

    body_parts = ["<!-- refurbed-watch-report -->"]
    if has_alerts:
        body_parts.append("## 🔔 New on refurbed.se\n")
        body_parts.append(alert_md)
        body_parts.append("\n---\n")
    body_parts.append("### Current standings\n")
    body_parts.append(standings_md or "_nothing listed_")
    body_parts.append("")
    archive_repo = os.environ.get(
        "GITHUB_REPOSITORY", "moonlitnayeem/refurbed-mac-watch"
    )
    body_parts.append(
        f"[View older updates](https://github.com/{archive_repo}/tree/main/history)"
    )
    body_parts.append("")
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    run_url = ""
    server, repo, run_id = (os.environ.get("GITHUB_SERVER_URL"),
                            os.environ.get("GITHUB_REPOSITORY"),
                            os.environ.get("GITHUB_RUN_ID"))
    if server and repo and run_id:
        run_url = f" · [workflow run]({server}/{repo}/actions/runs/{run_id})"
    body_parts.append(f"<sub>Checked {stamp}{run_url}</sub>")

    Path("alert.md").write_text("\n".join(body_parts), encoding="utf-8")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        n = len(COLLECTED_ALERTS)
        first = COLLECTED_ALERTS[0] if COLLECTED_ALERTS else None
        if n == 1 and first:
            title = f"{first['subtitle']} — {first['message']}"
        elif n > 1:
            title = f"{n} new refurbed listings"
        else:
            title = "No change"
        # Issue titles must be single-line and reasonably short.
        title = " ".join(title.split())[:120]
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"has_alerts={'true' if has_alerts else 'false'}\n")
            f.write(f"alert_title={title}\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(body_parts) + "\n")
            if status:
                f.write("\n<details><summary>Status</summary>\n\n```\n"
                        + "\n".join(status) + "\n```\n</details>\n")


def write_phone_alert_file(path: Path = Path("phone_alerts.json")) -> None:
    """Write pending Telegram alerts for the workflow, or remove stale output."""
    if PHONE_ALERTS:
        path.write_text(
            json.dumps(PHONE_ALERTS, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    global NOTIFY_BACKEND

    ap = argparse.ArgumentParser(description="Watch refurbed.se for Apple hardware.")
    ap.add_argument("--dry-run", action="store_true",
                    help="check and print, but do not notify or save state")
    ap.add_argument("--list", action="store_true", help="print current matches and exit")
    ap.add_argument("--reset", action="store_true",
                    help="delete saved state so the next run re-baselines")
    ap.add_argument("--state", default=os.environ.get("WATCH_STATE", "state.json"),
                    help="path to the state file (default: ./state.json)")
    ap.add_argument("--notify", choices=["auto", "github", "macos", "none"], default="auto",
                    help="notification backend (default: auto-detect)")
    ap.add_argument("--verbose", "-v", action="store_true", help="log to stderr")
    args = ap.parse_args()

    NOTIFY_BACKEND = args.notify
    state_path = Path(args.state).expanduser()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
    if not (args.verbose or args.dry_run or args.list
            or os.environ.get("GITHUB_ACTIONS") == "true"):
        logging.disable(logging.INFO)

    if args.reset:
        if state_path.exists():
            state_path.unlink()
            print(f"Removed {state_path}. Next run will re-baseline.")
        else:
            print("No state file to remove.")
        return 0

    if args.list:
        for w in WATCHES:
            page = fetch(search_url(w.query))
            if page is None:
                print(f"\n{w.label}: fetch failed")
                continue
            offers = parse_search_page(page)
            print(f"\n{w.label}  ({len(offers)} raw results for {w.query!r})")
            for o in offers:
                if w.needs_config:
                    enrich(o)
                mark = "OK " if w.matches(o) else "no "
                print(f"  {mark} {fmt_kr(o.price):>12}  {o.label}")
                print(f"       {o.url}")
        return 0

    state = load_state(state_path)
    if "price_lows" not in state:
        state["price_lows"] = load_archived_price_lows()
    notifications_left = [MAX_NOTIFICATIONS_PER_RUN]
    all_lines: list[str] = []
    active_watch_keys: list[str] = []

    for w in WATCHES:
        try:
            previous_check = state["watches"].get(w.key, {}).get("checked_at")
            all_lines += run_watch(w, state, args.dry_run, notifications_left)
            current_check = state["watches"].get(w.key, {}).get("checked_at")
            if not args.dry_run and current_check and current_check != previous_check:
                active_watch_keys.append(w.key)
        except Exception as e:  # noqa: BLE001 - one broken watch must not stop the others
            logging.exception("[%s] unhandled error: %s", w.key, e)
            all_lines.append(f"{w.label}: error ({e})")

    all_lines = build_buy_now_lines(
        state,
        active_watch_keys=active_watch_keys if not args.dry_run else None,
    ) + all_lines
    write_phone_alert_file()

    if not args.dry_run:
        screenshot_requests = update_price_lows(
            state, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        )
        write_price_proof_requests(screenshot_requests)
        save_state(state_path, state)

    alert_md, standings_md = build_report(all_lines)
    status = [ln for ln in all_lines
              if not ln.startswith("__STANDINGS__") and not ln.startswith("__ITEM__")]

    for line in status:
        print(line)
    if standings_md:
        print("\n" + standings_md)

    if resolve_backend() == "github":
        write_github_outputs(alert_md, standings_md, status)

    # Always exit 0 so the scheduler does not treat a quiet run as a failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
