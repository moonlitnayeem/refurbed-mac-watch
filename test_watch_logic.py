#!/usr/bin/env python3
"""
Tests for the run-to-run diffing: what actually fires an alert.

Network and notifications are stubbed, so these run offline and assert on the
behaviour that matters -- that the watcher is quiet when it should be quiet,
and loud exactly once when something real happens.
"""

import os
import unittest
import tempfile
from pathlib import Path
from unittest import mock

import refurbed_watch as rw
from test_parser import card, TITLE_M3_MAX_64, TITLE_M4_MAX_36

M2_MAX_64_TITLE = ('<title>Apple MacBook Pro 2023 Apple M2 Max 12 Core '
                   '64.0 GB 2000 GB – refurbed</title>')
STUDIO_M1_TITLE = ('<title>Apple Mac Studio 2022 Apple M1 Max 10 Core '
                   '32.0 GB 512 GB – refurbed</title>')
STUDIO_ULTRA_TITLE = ('<title>Apple Mac Studio 2025 Apple M3 Ultra 28 Core '
                      '256.0 GB 1000 GB – refurbed</title>')

P_M3 = "/p/apple-macbook-pro-2023-m3-16-2/258184c/"
P_M4 = "/p/apple-macbook-pro-2024-m4-14/282199c/"   # M4 Max but only 36 GB
P_M2 = "/p/apple-macbook-pro-2023-m2-14/167837c/"
P_M1_DK = "/p/apple-macbook-pro-2021-m1-14/209635c/"
P_STUDIO = "/p/apple-mac-studio-2022-m1-max/72461aa/"
P_ULTRA = "/p/apple-mac-studio-2025-m3-ultra/99001a/"
P_MINI = "/p/apple-mac-mini-2024-m4/273548aa/"      # fuzzy match, not a Mac Studio
P_MACBOOK_32 = "/p/apple-macbook-air-2024-m3-15/300001a/"
P_IMAC_INTEL = "/p/apple-imac-2020-intel/300002a/"
P_IPAD_32 = "/p/apple-ipad-pro-2024-m4/300003a/"


def search_html(entries):
    return "<html><body>" + "".join(
        card(path, "Apple Product", "Bara ett fåtal kvar", price, "57 192,05 kr")
        for path, price in entries
    ) + "</body></html>"


class FakeSite:
    """Serves canned search + variant pages, and counts requests."""

    def __init__(self, entries, titles=None):
        self.entries = entries
        self.titles = titles or {
            P_M3: TITLE_M3_MAX_64, P_M4: TITLE_M4_MAX_36, P_M2: M2_MAX_64_TITLE,
            P_STUDIO: STUDIO_M1_TITLE, P_ULTRA: STUDIO_ULTRA_TITLE,
            P_MINI: '<title>Apple Mac Mini 2024 Apple M4 10 Core 16.0 GB 256 GB – refurbed</title>',
        }
        self.requests = []

    def fetch(self, url):
        self.requests.append(url)
        if "/search/" in url:
            return search_html(self.entries)
        for path, title in self.titles.items():
            if url.endswith(path):
                return title
        return None


class BaseWatchTest(unittest.TestCase):
    watch_kwargs: dict = {}

    def setUp(self):
        rw.COLLECTED_ALERTS.clear()
        self.watch = rw.Watch(**self.watch_kwargs)
        self.state = {"version": 2, "watches": {}}
        self.notes = []

    def run_once(self, site, budget=None):
        with mock.patch.object(rw, "fetch", side_effect=site.fetch), \
             mock.patch.object(rw, "notify",
                               side_effect=lambda t, s, m, url=None: self.notes.append((t, s, m, url))):
            return rw.run_watch(self.watch, self.state, dry_run=False,
                                notifications_left=[budget or rw.MAX_NOTIFICATIONS_PER_RUN])


class BestValueMacWatchTest(BaseWatchTest):
    watch_kwargs = dict(key="best-value-macs", label="Best-value Macs · 32 GB+",
                        query="Apple Mac 32 GB", matches=rw.is_best_value_mac,
                        needs_config=True, offer_label=rw.best_value_offer_label)

    def test_only_priced_apple_silicon_macs_with_at_least_32gb_qualify(self):
        mini_32 = rw.Offer(P_MINI, price=12_000, chip="M4", ram_gb=32)
        macbook_64 = rw.Offer(P_MACBOOK_32, price=18_000, chip="M3 Pro", ram_gb=64)
        cases = [
            (mini_32, True),
            (macbook_64, True),
            (rw.Offer(P_MINI, price=9_000, chip="M4", ram_gb=16), False),
            (rw.Offer(P_IMAC_INTEL, price=8_000, chip="", ram_gb=32), False),
            (rw.Offer(P_IPAD_32, price=7_000, chip="M4", ram_gb=32), False),
            (rw.Offer(P_MINI, price=None, chip="M4", ram_gb=32), False),
        ]

        for offer, expected in cases:
            with self.subTest(path=offer.path, chip=offer.chip, ram=offer.ram_gb):
                self.assertEqual(rw.is_best_value_mac(offer), expected)

    def test_category_is_first_and_standings_prioritize_lowest_price(self):
        self.assertEqual(rw.WATCHES[0].key, "best-value-macs")
        titles = {
            P_MINI: ('<title>Apple Mac mini 2024 Apple M4 10 Core '
                     '32.0 GB 512 GB – refurbed</title>'),
            P_MACBOOK_32: ('<title>Apple MacBook Air 2024 Apple M3 8 Core '
                           '32.0 GB 512 GB – refurbed</title>'),
            P_IMAC_INTEL: ('<title>Apple iMac 2020 Intel Core i7 '
                           '32.0 GB 512 GB – refurbed</title>'),
        }
        site = FakeSite(
            [(P_MACBOOK_32, "18 000 kr"), (P_IMAC_INTEL, "8 000 kr"),
             (P_MINI, "12 000 kr")],
            titles=titles,
        )

        lines = self.run_once(site)
        items = [line for line in lines if line.startswith("__ITEM__[")]

        self.assertEqual(len(items), 2)
        self.assertIn("Mac mini", items[0])
        self.assertIn("12 000 kr", items[0])
        self.assertIn("MacBook Air", items[1])
        self.assertIn("18 000 kr", items[1])


class MacBookProWatchTest(BaseWatchTest):
    watch_kwargs = dict(key="mbp", label="MacBook Pro Max 64 GB",
                        query="MacBook Pro Max 64 GB",
                        matches=rw.is_max_or_ultra_64gb, needs_config=True)

    def test_first_run_is_silent_baseline(self):
        site = FakeSite([(P_M3, "39 499 kr"), (P_M4, "36 405 kr")])
        lines = self.run_once(site)
        self.assertEqual(self.notes, [])
        self.assertIn("baseline", " ".join(lines))
        offers = self.state["watches"]["mbp"]["offers"]
        self.assertTrue(offers[P_M3]["matched"])
        self.assertFalse(offers[P_M4]["matched"])

    def test_unchanged_second_run_is_silent(self):
        site = FakeSite([(P_M3, "39 499 kr"), (P_M4, "36 405 kr")])
        self.run_once(site)
        self.notes.clear()
        lines = self.run_once(site)
        self.assertEqual(self.notes, [])
        self.assertIn("no change", " ".join(lines).lower())

    def test_new_matching_listing_notifies_once(self):
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        self.notes.clear()

        site.entries = [(P_M3, "39 499 kr"), (P_M2, "31 999 kr")]
        self.run_once(site)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("New", self.notes[0][0])
        self.assertIn("M2 Max", self.notes[0][1])
        self.assertTrue(self.notes[0][3].endswith(P_M2), "alert must carry the product URL")

        self.notes.clear()
        self.run_once(site)
        self.assertEqual(self.notes, [], "must not re-alert on the following run")

    def test_extra_query_discovers_collapsed_alternate_configuration(self):
        """Refurbed search cards hide alternate storage/keyboard variants."""
        self.watch.extra_queries = (
            "Apple MacBook Pro 2021 M1 Max 64 GB 1 TB DK",
        )
        exact_query = rw.search_url(self.watch.extra_queries[0])
        title = ('<title>Apple MacBook Pro 2021 M1 Apple M1 Max 10 Core '
                 '64.0 GB 1000 GB 14.2 " silver DK (Dansk) – refurbed</title>')

        def fetch(url):
            if url == exact_query:
                return search_html([(P_M1_DK, "25 325 kr")])
            if "/search/" in url:
                return search_html([(P_M3, "39 499 kr")])
            if url.endswith(P_M1_DK):
                return title
            if url.endswith(P_M3):
                return TITLE_M3_MAX_64
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False,
                         notifications_left=[rw.MAX_NOTIFICATIONS_PER_RUN])

        offers = self.state["watches"]["mbp"]["offers"]
        self.assertIn(P_M1_DK, offers)
        self.assertTrue(offers[P_M1_DK]["matched"])

    def test_new_non_matching_listing_is_silent(self):
        """A 36 GB M4 Max appearing must not wake anyone up."""
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        self.notes.clear()
        site.entries = [(P_M3, "39 499 kr"), (P_M4, "36 405 kr")]
        self.run_once(site)
        self.assertEqual(self.notes, [])

    def test_price_drop_notifies(self):
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        self.notes.clear()
        site.entries = [(P_M3, "35 000 kr")]
        self.run_once(site)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Price drop", self.notes[0][0])
        self.assertIn("39 499 kr", self.notes[0][2])
        self.assertIn("35 000 kr", self.notes[0][2])

    def test_price_increase_is_silent(self):
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        self.notes.clear()
        site.entries = [(P_M3, "42 000 kr")]
        self.run_once(site)
        self.assertEqual(self.notes, [])

    def test_multiple_alerts_are_sorted_lowest_price_first(self):
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        self.notes.clear()
        site.titles[P_M1_DK] = (
            '<title>Apple MacBook Pro 2021 M1 Apple M1 Max 10 Core '
            '64.0 GB 1000 GB 14.2 " silver DK (Dansk) – refurbed</title>'
        )
        # Deliberately return the expensive listing first.
        site.entries = [(P_M3, "39 499 kr"), (P_M2, "31 999 kr"),
                        (P_M1_DK, "25 325 kr")]

        self.run_once(site)

        self.assertEqual([note[2].split(" ·")[0] for note in self.notes],
                         ["25 325 kr", "31 999 kr"])

    def test_listing_disappearing_is_silent(self):
        site = FakeSite([(P_M3, "39 499 kr"), (P_M2, "31 999 kr")])
        self.run_once(site)
        self.notes.clear()
        site.entries = [(P_M3, "39 499 kr")]
        self.run_once(site)
        self.assertEqual(self.notes, [])

    def test_zero_results_treated_as_anomaly(self):
        """Site hiccup must not wipe state or read as a mass sell-out."""
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        before = dict(self.state["watches"]["mbp"]["offers"])
        self.notes.clear()

        site.entries = []
        lines = self.run_once(site)
        self.assertEqual(self.notes, [])
        self.assertIn("anomaly", " ".join(lines).lower())
        self.assertEqual(self.state["watches"]["mbp"]["offers"], before)

        site.entries = [(P_M3, "39 499 kr")]
        self.run_once(site)
        self.assertEqual(self.notes, [], "recovery must not read as a new listing")

    def test_search_failure_is_survivable(self):
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        self.notes.clear()
        with mock.patch.object(rw, "fetch", return_value=None):
            lines = rw.run_watch(self.watch, self.state, dry_run=False,
                                 notifications_left=[8])
        self.assertEqual(self.notes, [])
        self.assertIn("fetch failed", " ".join(lines))

    def test_config_is_cached_not_refetched(self):
        """Verified variants must not be re-fetched on every run."""
        site = FakeSite([(P_M3, "39 499 kr"), (P_M4, "36 405 kr")])
        self.run_once(site)
        self.assertEqual(len([u for u in site.requests if "/search/" not in u]), 2)
        site.requests.clear()
        self.run_once(site)
        self.assertEqual([u for u in site.requests if "/search/" not in u], [],
                         "cached configs should mean zero variant fetches")

    def test_notification_burst_is_capped(self):
        site = FakeSite([(P_M3, "39 499 kr")])
        self.run_once(site)
        self.notes.clear()
        extra = [(f"/p/apple-macbook-pro-2023-m2-14/x{i}/", "31 999 kr") for i in range(10)]
        for path, _ in extra:
            site.titles[path] = M2_MAX_64_TITLE
        site.entries = [(P_M3, "39 499 kr")] + extra
        self.run_once(site, budget=3)
        self.assertEqual(len(self.notes), 3)

    def test_dry_run_does_not_persist_or_notify(self):
        site = FakeSite([(P_M3, "39 499 kr")])
        with mock.patch.object(rw, "fetch", side_effect=site.fetch), \
             mock.patch.object(rw, "notify",
                               side_effect=lambda t, s, m, url=None: self.notes.append(t)):
            rw.run_watch(self.watch, self.state, dry_run=True, notifications_left=[8])
        self.assertEqual(self.notes, [])
        self.assertEqual(self.state["watches"], {})


class MacStudioWatchTest(BaseWatchTest):
    """Search lists only purchasable offers, so appearing at all means buyable."""

    watch_kwargs = dict(key="mac-studio", label="Mac Studio", query="Mac Studio",
                        matches=rw.is_mac_studio, needs_config=True)

    def test_fuzzy_matches_never_alert(self):
        """A Mac mini in the results must not be reported as a Mac Studio."""
        site = FakeSite([(P_MINI, "11 615 kr")])
        lines = self.run_once(site)          # baseline
        self.notes.clear()
        site.entries = [(P_MINI, "9 999 kr")]  # even a big price drop
        self.run_once(site)
        self.assertEqual(self.notes, [])
        self.assertIn("Mac Studio (0)", " ".join(lines))

    def test_restock_notifies(self):
        site = FakeSite([(P_STUDIO, "19 845 kr")])
        self.run_once(site)
        self.notes.clear()
        site.entries = [(P_STUDIO, "19 845 kr"), (P_ULTRA, "54 900 kr")]
        self.run_once(site)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("M3 Ultra", self.notes[0][1])

    def test_first_studio_after_empty_period_notifies(self):
        """Today's real state: zero available. One appearing is the whole point."""
        site = FakeSite([(P_MINI, "11 615 kr")])
        self.run_once(site)
        self.notes.clear()
        site.entries = [(P_MINI, "11 615 kr"), (P_STUDIO, "19 845 kr")]
        self.run_once(site)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("M1 Max", self.notes[0][1])


class ReportTest(unittest.TestCase):
    def setUp(self):
        rw.COLLECTED_ALERTS.clear()

    def test_standings_render_as_links(self):
        lines = ["Mac Studio: no change (1 matches)",
                 "__STANDINGS__Mac Studio (1)",
                 "__ITEM__[M1 Max · 32 GB](https://example.com/x) — 19 845 kr"]
        alert, standings = rw.build_report(lines)
        self.assertEqual(alert, "")
        self.assertIn("**Mac Studio (1)**", standings)
        self.assertIn("- [M1 Max · 32 GB](https://example.com/x)", standings)

    def test_alert_block_includes_link(self):
        rw.COLLECTED_ALERTS.append(
            {"title": "New: Mac Studio", "subtitle": "M1 Max · 32 GB",
             "message": "19 845 kr", "url": "https://example.com/x"})
        alert, _ = rw.build_report(["__STANDINGS__Mac Studio (1)"])
        self.assertIn("**NEW**", alert)
        self.assertIn("[M1 Max · 32 GB](https://example.com/x)", alert)

    def test_price_drop_labelled(self):
        rw.COLLECTED_ALERTS.append(
            {"title": "Price drop: Mac Studio", "subtitle": "M1 Max",
             "message": "19 845 kr → 17 500 kr", "url": "https://example.com/x"})
        alert, _ = rw.build_report([])
        self.assertIn("**PRICE DROP**", alert)

    def test_github_report_has_history_marker(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                rw.write_github_outputs("", "standings", [])
                body = Path("alert.md").read_text(encoding="utf-8")
            finally:
                os.chdir(old_cwd)
        self.assertTrue(body.startswith("<!-- refurbed-watch-report -->"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
