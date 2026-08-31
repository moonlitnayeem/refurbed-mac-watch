#!/usr/bin/env python3
"""
Tests for the run-to-run diffing: what actually fires an alert.

Network and notifications are stubbed, so these run offline and assert on the
behaviour that matters -- that the watcher is quiet when it should be quiet,
and loud exactly once when something real happens.
"""

import io
import json
import os
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import refurbed_watch as rw
from test_parser import card, TITLE_M3_MAX_64, TITLE_M4_MAX_36

M2_MAX_64_TITLE = ('<title>Apple MacBook Pro 2023 Apple M2 Max 12 Core '
                   '64.0 GB 2000 GB – refurbed</title>')
STUDIO_M1_TITLE = ('<title>Apple Mac Studio 2022 Apple M1 Max 10 Core '
                   '32.0 GB 512 GB – refurbed</title>')
STUDIO_M1_ULTRA_TITLE = ('<title>Apple Mac Studio 2022 Apple M1 Ultra 20 Core '
                         '128.0 GB 2000 GB – refurbed</title>')
STUDIO_M1_ULTRA_64_TITLE = ('<title>Apple Mac Studio 2022 Apple M1 Ultra 20 Core '
                            '64.0 GB 1000 GB – refurbed</title>')
STUDIO_M1_ULTRA_OUT_OF_STOCK = (
    STUDIO_M1_ULTRA_TITLE
    + "<p>Produkten finns för närvarande inte i lager</p>"
)
STUDIO_ULTRA_TITLE = ('<title>Apple Mac Studio 2025 Apple M3 Ultra 28 Core '
                      '256.0 GB 1000 GB – refurbed</title>')

P_M3 = "/p/apple-macbook-pro-2023-m3-16-2/258184c/"
P_M4 = "/p/apple-macbook-pro-2024-m4-14/282199c/"   # M4 Max but only 36 GB
P_M2 = "/p/apple-macbook-pro-2023-m2-14/167837c/"
P_M1_DK = "/p/apple-macbook-pro-2021-m1-14/209635c/"
P_STUDIO = "/p/apple-mac-studio-2022-m1-max/72461aa/"
P_M1_ULTRA_FAMILY = "/p/apple-mac-studio-2022-m1-ultra/"
P_M1_ULTRA_VARIANT = "/p/apple-mac-studio-2022-m1-ultra/12345a/"
P_M1_ULTRA_VARIANT_B = "/p/apple-mac-studio-2022-m1-ultra/67890b/"
P_ULTRA = "/p/apple-mac-studio-2025-m3-ultra/99001a/"
P_ULTRA_GENERIC_SLUG = "/p/apple-mac-studio-2025/abc123/"
P_MINI = "/p/apple-mac-mini-2024-m4/273548aa/"      # fuzzy match, not a Mac Studio
P_MACBOOK_32 = "/p/apple-macbook-air-2024-m3-15/300001a/"
P_IMAC_INTEL = "/p/apple-imac-2020-intel/300002a/"
P_IPAD_32 = "/p/apple-ipad-pro-2024-m4/300003a/"
P_M2_96 = "/p/apple-macbook-pro-2023-m2-16-2/302400b/"
P_M2_96_BASE = "/p/apple-macbook-pro-2023-m2-16-2/75335b/"
P_M2_ALT_CONFIG = "/p/apple-macbook-pro-2023-m2-16-2/92672b/"
P_IMAC_64 = "/p/apple-imac-2026-m5/300004a/"


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
        if hasattr(rw, "PHONE_ALERTS"):
            rw.PHONE_ALERTS.clear()
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


class AppleSilicon64PlusWatchTest(BaseWatchTest):
    watch_kwargs = dict(
        key="apple-silicon-64-plus",
        label="Apple silicon Macs · 64 GB+",
        query="Apple Mac 64 GB",
        matches=rw.is_apple_silicon_64gb_or_more,
        needs_config=True,
        discover_ram_variants=True,
        offer_label=rw.best_value_offer_label,
    )

    def test_accepts_any_apple_silicon_mac_with_64gb_or_more(self):
        self.assertTrue(rw.is_apple_silicon_64gb_or_more(
            rw.Offer(P_M2_96, price=41_459, chip="M2 Max", ram_gb=96)))
        self.assertTrue(rw.is_apple_silicon_64gb_or_more(
            rw.Offer(P_IMAC_64, price=30_000, chip="M5", ram_gb=64)))
        self.assertFalse(rw.is_apple_silicon_64gb_or_more(
            rw.Offer(P_M2_96, price=20_000, chip="M2 Max", ram_gb=32)))
        self.assertFalse(rw.is_apple_silicon_64gb_or_more(
            rw.Offer(P_IMAC_64, price=20_000, chip="", ram_gb=128)))

    def test_threshold_target_is_exact_m2_max_64gb_macbook_pro_below_23000(self):
        self.assertTrue(rw.is_m2_max_64gb_under_threshold(
            rw.Offer(P_M2, price=22_999, chip="M2 Max", ram_gb=64)))

        rejected = [
            rw.Offer(P_M2, price=23_000, chip="M2 Max", ram_gb=64),
            rw.Offer(P_M2, price=22_999, chip="M2 Max", ram_gb=96),
            rw.Offer(P_M2, price=22_999, chip="M2 Pro", ram_gb=64),
            rw.Offer(P_STUDIO, price=22_999, chip="M2 Max", ram_gb=64),
        ]
        for offer in rejected:
            with self.subTest(path=offer.path, price=offer.price,
                              chip=offer.chip, ram=offer.ram_gb):
                self.assertFalse(rw.is_m2_max_64gb_under_threshold(offer))

    def test_m_series_max_threshold_accepts_any_generation_and_64gb_or_more(self):
        accepted = [
            rw.Offer(P_M1_DK, price=17_999, chip="M1 Max", ram_gb=64),
            rw.Offer(P_M2_96, price=17_999, chip="M3 Max", ram_gb=96),
            rw.Offer(P_M3, price=1, chip="M12 Max", ram_gb=128),
        ]
        for offer in accepted:
            with self.subTest(chip=offer.chip, ram=offer.ram_gb):
                self.assertTrue(
                    rw.is_m_series_max_macbook_pro_64gb_plus_under_threshold(offer)
                )

        rejected = [
            rw.Offer(P_M3, price=18_000, chip="M3 Max", ram_gb=64),
            rw.Offer(P_M3, price=17_999, chip="M3 Max", ram_gb=63),
            rw.Offer(P_M3, price=17_999, chip="M3 Pro", ram_gb=96),
            rw.Offer(P_M3, price=17_999, chip="M3 Ultra", ram_gb=96),
            rw.Offer(P_STUDIO, price=17_999, chip="M3 Max", ram_gb=96),
            rw.Offer(P_MACBOOK_32, price=17_999, chip="M3 Max", ram_gb=96),
            rw.Offer(P_M3, price=None, chip="M3 Max", ram_gb=96),
        ]
        for offer in rejected:
            with self.subTest(path=offer.path, price=offer.price,
                              chip=offer.chip, ram=offer.ram_gb):
                self.assertFalse(
                    rw.is_m_series_max_macbook_pro_64gb_plus_under_threshold(offer)
                )

    def test_dedicated_max_query_discovers_listing_missing_from_broad_searches(self):
        self.watch = next(
            watch for watch in rw.WATCHES
            if watch.key == "apple-silicon-64-plus"
        )
        targeted_query = rw.search_url("Apple MacBook Pro Max 64 GB")
        requests = []

        def fetch(url):
            requests.append(url)
            if url == targeted_query:
                return search_html([(P_M3, "17 999 kr")])
            if "/search/" in url:
                return search_html([])
            if url.endswith(P_M3):
                return TITLE_M3_MAX_64
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(
                self.watch,
                self.state,
                dry_run=False,
                notifications_left=[rw.MAX_NOTIFICATIONS_PER_RUN],
            )
            rw.run_watch(
                self.watch,
                self.state,
                dry_run=False,
                notifications_left=[rw.MAX_NOTIFICATIONS_PER_RUN],
            )

        self.assertIn(targeted_query, requests)
        offer = self.state["watches"][self.watch.key]["offers"][P_M3]
        self.assertTrue(offer["matched"])
        self.assertEqual(offer["price"], 17_999)
        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "the silent initial baseline must persist the rule qualification",
        )

    def test_qualifying_offer_reappearance_sends_one_threshold_phone_alert(self):
        site = FakeSite([(P_M3, "39 499 kr"), (P_M2, "22 999 kr")])
        self.run_once(site)
        self.assertEqual(rw.PHONE_ALERTS, [], "initial baseline must stay silent")

        site.entries = [(P_M3, "39 499 kr")]
        self.run_once(site)
        self.assertEqual(rw.PHONE_ALERTS, [])

        site.entries = [(P_M3, "39 499 kr"), (P_M2, "22 999 kr")]
        self.run_once(site)

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        alert = rw.PHONE_ALERTS[0]
        self.assertEqual(alert["title"], "M2 Max 64 GB below 23 000 kr")
        self.assertEqual(alert["model"], "MacBook Pro · M2 Max · 64 GB · 2000 GB SSD")
        self.assertEqual(alert["price"], 22_999)
        self.assertTrue(alert["url"].endswith(P_M2))

    def test_crossing_below_threshold_sends_once_while_further_drops_stay_silent(self):
        site = FakeSite([(P_M2, "23 000 kr")])
        self.run_once(site)

        site.entries = [(P_M2, "22 999 kr")]
        self.run_once(site)
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(rw.PHONE_ALERTS[0]["price"], 22_999)

        site.entries = [(P_M2, "22 500 kr")]
        self.run_once(site)
        self.assertEqual(
            len(rw.PHONE_ALERTS), 1,
            "an unchanged below-threshold state must not repeat every run",
        )

    def test_m_series_max_crossing_below_18000_sends_once(self):
        site = FakeSite([(P_M3, "18 000 kr")])
        self.run_once(site)

        site.entries = [(P_M3, "17 999 kr")]
        self.run_once(site)

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        alert = rw.PHONE_ALERTS[0]
        self.assertEqual(
            alert["title"],
            "M-series Max MacBook Pro 64 GB+ below 18 000 kr",
        )
        self.assertEqual(
            alert["model"],
            "MacBook Pro · M3 Max · 64 GB · 2000 GB SSD",
        )
        self.assertEqual(alert["price"], 17_999)
        self.assertTrue(alert["url"].endswith(P_M3))

        site.entries = [(P_M3, "17 500 kr")]
        self.run_once(site)
        self.assertEqual(
            len(rw.PHONE_ALERTS),
            1,
            "remaining below 18 000 kr must not repeat every run",
        )

    def test_existing_qualifying_listing_alerts_once_when_rule_is_introduced(self):
        self.state["watches"][self.watch.key] = {
            "offers": {
                P_M3: {
                    "name": "Apple MacBook Pro 2023 M3",
                    "price": 17_999,
                    "config": "Apple MacBook Pro 2023 M3 Max 64 GB",
                    "chip": "M3 Max",
                    "ram_gb": 64.0,
                    "ssd_gb": 2000.0,
                    "matched": True,
                }
            }
        }
        site = FakeSite([(P_M3, "17 999 kr")])

        self.run_once(site)
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "M-series Max MacBook Pro 64 GB+ below 18 000 kr",
        )

        self.run_once(site)
        self.assertEqual(
            len(rw.PHONE_ALERTS),
            1,
            "the migrated qualifying listing must not repeat on the next run",
        )

    def test_overlapping_m2_rules_send_only_the_18000_alert(self):
        site = FakeSite([(P_M2, "23 000 kr")])
        self.run_once(site)

        site.entries = [(P_M2, "17 999 kr")]
        self.run_once(site)

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "M-series Max MacBook Pro 64 GB+ below 18 000 kr",
        )

    def test_discovers_96gb_variant_hidden_in_ram_dropdown(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        hidden = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>41 305 kr</span></p>'
            '<select data-test="keyboard">'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/75335b/'
            '?offer=19861870" selected data-price="">EN (QWERTY)</option>'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/302400b/'
            '?offer=19862087" data-price="more,+154 kr">US (QWERTY)</option>'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/92672b/'
            '?offer=15407428" data-type="optgroup-option" '
            'data-price="less,-23 054 kr">UK (QWERTY)</option>'
            '</select>'
        )
        exact_sibling = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>41 459 kr</span></p>'
        )
        site = FakeSite(
            [(P_M2, "30 000 kr")],
            titles={P_M2: representative, P_M2_96_BASE: hidden,
                    P_M2_96: exact_sibling},
        )

        lines = self.run_once(site)
        offers = self.state["watches"]["apple-silicon-64-plus"]["offers"]

        self.assertTrue(offers[P_M2_96]["matched"])
        self.assertIn(rw.BASE + P_M2_96, site.requests)
        self.assertEqual(offers[P_M2_96]["ram_gb"], 96.0)
        self.assertEqual(offers[P_M2_96]["price"], 41459)
        self.assertNotIn(P_M2_ALT_CONFIG, offers)
        self.assertIn("96 GB", "\n".join(lines))

    def test_hidden_variant_representative_failure_preserves_alert_state(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        hidden = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        malformed = (
            '<html><title>Temporary Mac page 96.0 GB 4000 GB</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p></html>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                return search_html([(P_M2, "30 000 kr")])
            if url.endswith(P_M2):
                if phase == "representative-failed":
                    return None
                if phase == "representative-malformed":
                    return malformed
                return representative
            if url.endswith(P_M2_96_BASE):
                return hidden
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "representative-failed"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertIn(P_M2_96_BASE, offers)
            preserved = offers[P_M2_96_BASE]
            self.assertTrue(preserved.get("verification_unknown"))
            self.assertIn(
                rw.M_SERIES_MAX_64GB_PLUS_ALERT_RULE,
                preserved.get("telegram_alert_rules", []),
            )

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            recovered = self.state["watches"][self.watch.key]["offers"][P_M2_96_BASE]
            self.assertFalse(recovered.get("verification_unknown"))

            phase = "representative-malformed"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertIn(P_M2_96_BASE, offers)
            self.assertTrue(offers[P_M2_96_BASE].get("verification_unknown"))

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "representative recovery must not duplicate the threshold alert",
        )

    def test_hidden_ram_destination_failure_is_unknown_not_stale_current(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        hidden = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        malformed = (
            '<html><title>Temporary Mac page 96.0 GB 4000 GB</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p></html>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                return search_html([(P_M2, "30 000 kr")])
            if url.endswith(P_M2):
                return representative
            if url.endswith(P_M2_96_BASE):
                if phase == "hidden-failed":
                    return None
                if phase == "hidden-malformed":
                    return malformed
                return hidden
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "hidden-failed"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            preserved = self.state["watches"][self.watch.key]["offers"][P_M2_96_BASE]
            self.assertTrue(preserved.get("verification_unknown"))
            self.assertIn(
                rw.M_SERIES_MAX_64GB_PLUS_ALERT_RULE,
                preserved.get("telegram_alert_rules", []),
            )

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            recovered = self.state["watches"][self.watch.key]["offers"][P_M2_96_BASE]
            self.assertFalse(recovered.get("verification_unknown"))

            phase = "hidden-malformed"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            preserved = self.state["watches"][self.watch.key]["offers"][P_M2_96_BASE]
            self.assertTrue(preserved.get("verification_unknown"))

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "hidden destination recovery must not duplicate the threshold alert",
        )

    def _assert_hidden_discovery_failure_is_product_family_scoped(
            self, failure_at: str, malformed: bool) -> None:
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        hidden = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        unrelated = (
            TITLE_M3_MAX_64
            + '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        malformed_page = (
            '<html><title>Temporary Mac page 96.0 GB 4000 GB</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p></html>'
        )
        phase = "baseline"

        def failed_page():
            return malformed_page if malformed else None

        def fetch(url):
            if "/search/" in url:
                entries = [(P_M2, "30 000 kr")]
                if phase in {"baseline", "recovered"}:
                    entries.append((P_M3, "17 999 kr"))
                return search_html(entries)
            if url.endswith(P_M2):
                if phase == "failure" and failure_at == "representative":
                    return failed_page()
                return representative
            if url.endswith(P_M2_96_BASE):
                if phase == "failure" and failure_at == "hidden":
                    return failed_page()
                return hidden
            if url.endswith(P_M3):
                return unrelated
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "failure"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertTrue(offers[P_M2_96_BASE].get("verification_unknown"))
            self.assertIn(
                rw.M_SERIES_MAX_64GB_PLUS_ALERT_RULE,
                offers[P_M2_96_BASE].get("telegram_alert_rules", []),
            )
            self.assertNotIn(
                P_M3,
                offers,
                "one failed product family must not mask an unrelated disappearance",
            )

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        alert_paths = {
            alert["url"].removeprefix(rw.BASE)
            for alert in rw.PHONE_ALERTS
        }
        self.assertEqual(
            alert_paths,
            {P_M3},
            "only the genuinely disappeared unrelated offer may alert on return",
        )

    def test_representative_fetch_failure_is_product_family_scoped(self):
        self._assert_hidden_discovery_failure_is_product_family_scoped(
            failure_at="representative", malformed=False,
        )

    def test_representative_malformed_page_is_product_family_scoped(self):
        self._assert_hidden_discovery_failure_is_product_family_scoped(
            failure_at="representative", malformed=True,
        )

    def test_hidden_destination_fetch_failure_is_product_family_scoped(self):
        self._assert_hidden_discovery_failure_is_product_family_scoped(
            failure_at="hidden", malformed=False,
        )

    def test_hidden_destination_malformed_page_is_product_family_scoped(self):
        self._assert_hidden_discovery_failure_is_product_family_scoped(
            failure_at="hidden", malformed=True,
        )

    def test_healthy_selector_removal_is_a_real_hidden_variant_disappearance(self):
        representative_with_hidden = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        representative_without_hidden = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        hidden = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                return search_html([(P_M2, "30 000 kr")])
            if url.endswith(P_M2):
                if phase == "disappeared":
                    return representative_without_hidden
                return representative_with_hidden
            if url.endswith(P_M2_96_BASE):
                return hidden
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "disappeared"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertNotIn(P_M2_96_BASE, offers)

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(rw.PHONE_ALERTS[0]["price"], 17_999)

    def test_representative_explicit_out_of_stock_is_known_unavailable(self):
        available = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        sold_out = (
            available
            + '<p>Produkten finns för närvarande inte i lager</p>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                return search_html([(P_M2_96_BASE, "17 999 kr")])
            if url.endswith(P_M2_96_BASE):
                return sold_out if phase == "sold-out" else available
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "sold-out"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertNotIn(P_M2_96_BASE, offers)

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(rw.PHONE_ALERTS[0]["price"], 17_999)

    def test_sold_out_representative_does_not_mask_unrelated_disappearance(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<select data-test="keyboard">'
            f'<option value="{P_M2_96_BASE}" selected data-price="">'
            'Nordic</option>'
            f'<option value="{P_M2_96}" data-price="more,+154 kr">'
            'US English</option></select>'
            '<p data-test="product-price"><span>17 845 kr</span></p>'
        )
        representative_sold_out = (
            representative
            + '<p>Produkten finns för närvarande inte i lager</p>'
        )
        exact_sibling = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        unrelated = (
            TITLE_M3_MAX_64
            + '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                entries = [(P_M2_96_BASE, "17 845 kr")]
                if phase in {"baseline", "recovered"}:
                    entries.append((P_M3, "17 999 kr"))
                return search_html(entries)
            if url.endswith(P_M2_96_BASE):
                if phase == "representative-sold-out":
                    return representative_sold_out
                return representative
            if url.endswith(P_M2_96):
                return exact_sibling
            if url.endswith(P_M3):
                return unrelated
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "representative-sold-out"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertNotIn(P_M2_96_BASE, offers)
            self.assertTrue(offers[P_M2_96].get("verification_unknown"))
            self.assertNotIn(P_M3, offers)

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        alert_paths = {
            alert["url"].removeprefix(rw.BASE)
            for alert in rw.PHONE_ALERTS
        }
        self.assertEqual(alert_paths, {P_M2_96_BASE, P_M3})

    def test_hidden_explicit_out_of_stock_is_known_unavailable(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        available = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        sold_out = (
            available
            + '<p>Produkten finns för närvarande inte i lager</p>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                return search_html([(P_M2, "30 000 kr")])
            if url.endswith(P_M2):
                return representative
            if url.endswith(P_M2_96_BASE):
                return sold_out if phase == "sold-out" else available
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "sold-out"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertNotIn(P_M2_96_BASE, offers)

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(rw.PHONE_ALERTS[0]["price"], 17_999)

    def test_sold_out_hidden_parent_preserves_unobserved_exact_sibling_unknown(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        hidden_parent_available = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 845 kr</span></p>'
            '<select data-test="keyboard">'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/75335b/'
            '?offer=19861870" selected data-price="">EN (QWERTY)</option>'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/302400b/'
            '?offer=19862087" data-price="more,+154 kr">US (QWERTY)</option>'
            '</select>'
        )
        hidden_parent_sold_out = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 845 kr</span></p>'
            '<p>Produkten finns för närvarande inte i lager</p>'
        )
        exact_sibling = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                return search_html([(P_M2, "30 000 kr")])
            if url.endswith(P_M2):
                return representative
            if url.endswith(P_M2_96_BASE):
                if phase == "sold-out":
                    return hidden_parent_sold_out
                return hidden_parent_available
            if url.endswith(P_M2_96):
                return exact_sibling
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "sold-out"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertNotIn(P_M2_96_BASE, offers)
            self.assertIn(P_M2_96, offers)
            self.assertTrue(offers[P_M2_96].get("verification_unknown"))
            self.assertIn(
                rw.M_SERIES_MAX_64GB_PLUS_ALERT_RULE,
                offers[P_M2_96].get("telegram_alert_rules", []),
            )

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertTrue(rw.PHONE_ALERTS[0]["url"].endswith(P_M2_96_BASE))

    def test_sold_out_hidden_parent_does_not_mask_unrelated_disappearance(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '64.0 GB 1000 GB – refurbed</title>'
            '<select><option value="/p/apple-macbook-pro-2023-m2-16-2/'
            '75335b/?offer=19861870">96.0 GB</option></select>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        hidden_parent_available = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 845 kr</span></p>'
            '<select data-test="keyboard">'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/75335b/'
            '?offer=19861870" selected data-price="">EN (QWERTY)</option>'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/302400b/'
            '?offer=19862087" data-price="more,+154 kr">US (QWERTY)</option>'
            '</select>'
        )
        hidden_parent_sold_out = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 845 kr</span></p>'
            '<p>Produkten finns för närvarande inte i lager</p>'
        )
        exact_sibling = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        m3_offer = (
            TITLE_M3_MAX_64
            + '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                entries = [(P_M2, "30 000 kr")]
                if phase in ("baseline", "recovered"):
                    entries.append((P_M3, "17 999 kr"))
                return search_html(entries)
            if url.endswith(P_M2):
                return representative
            if url.endswith(P_M2_96_BASE):
                if phase == "sold-out":
                    return hidden_parent_sold_out
                return hidden_parent_available
            if url.endswith(P_M2_96):
                return exact_sibling
            if url.endswith(P_M3):
                return m3_offer
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "sold-out"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            offers = self.state["watches"][self.watch.key]["offers"]
            self.assertNotIn(P_M2_96_BASE, offers)
            self.assertTrue(offers[P_M2_96].get("verification_unknown"))
            self.assertNotIn(P_M3, offers)

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        alerted_paths = {alert["url"].removeprefix(rw.BASE) for alert in rw.PHONE_ALERTS}
        self.assertEqual(alerted_paths, {P_M2_96_BASE, P_M3})

    def test_incomplete_nonrepresentative_exact_page_cannot_alert(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>30 000 kr</span></p>'
        )
        incomplete = (
            '<title>Apple MacBook Pro 2023 Apple M3 Max 16 Core '
            '96.0 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        entries = [(P_M2_96_BASE, "30 000 kr")]

        def fetch(url):
            if "/search/" in url:
                return search_html(entries)
            if url.endswith(P_M2_96_BASE):
                return representative
            if url.endswith(P_M2_96):
                return incomplete
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            entries.append((P_M2_96, "17 999 kr"))
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        offers = self.state["watches"][self.watch.key]["offers"]
        self.assertNotIn(P_M2_96, offers)
        self.assertEqual(rw.PHONE_ALERTS, [])

    def test_hidden_exact_verification_failure_preserves_alert_state(self):
        representative = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 845 kr</span></p>'
            '<select data-test="keyboard">'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/75335b/'
            '?offer=19861870" selected data-price="">EN (QWERTY)</option>'
            '<option value="/p/apple-macbook-pro-2023-m2-16-2/302400b/'
            '?offer=19862087" data-price="more,+154 kr">US (QWERTY)</option>'
            '</select>'
        )
        exact_sibling = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p>'
        )
        exact_no_price = (
            '<title>Apple MacBook Pro 2023 M2 Apple M2 Max 12 Core '
            '96.0 GB 4000 GB – refurbed</title>'
        )
        malformed = (
            '<html><title>Temporary Mac page 96.0 GB 4000 GB</title>'
            '<p data-test="product-price"><span>17 999 kr</span></p></html>'
        )
        phase = "baseline"

        def fetch(url):
            if "/search/" in url:
                return search_html([(P_M2_96_BASE, "17 845 kr")])
            if url.endswith(P_M2_96_BASE):
                return representative
            if url.endswith(P_M2_96):
                if phase == "exact-failed":
                    return None
                if phase == "exact-malformed":
                    return malformed
                if phase == "exact-no-price":
                    return exact_no_price
                return exact_sibling
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(rw.PHONE_ALERTS, [])

            phase = "exact-failed"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            preserved = self.state["watches"][self.watch.key]["offers"][P_M2_96]
            self.assertTrue(preserved.get("verification_unknown"))
            self.assertIn(
                rw.M_SERIES_MAX_64GB_PLUS_ALERT_RULE,
                preserved.get("telegram_alert_rules", []),
            )

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            recovered = self.state["watches"][self.watch.key]["offers"][P_M2_96]
            self.assertFalse(recovered.get("verification_unknown"))

            phase = "exact-malformed"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            preserved = self.state["watches"][self.watch.key]["offers"][P_M2_96]
            self.assertTrue(preserved.get("verification_unknown"))

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            recovered = self.state["watches"][self.watch.key]["offers"][P_M2_96]
            self.assertFalse(recovered.get("verification_unknown"))

            phase = "exact-no-price"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            preserved = self.state["watches"][self.watch.key]["offers"][P_M2_96]
            self.assertTrue(preserved.get("verification_unknown"))

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "exact verification recovery must not duplicate the threshold alert",
        )


class PriceHistoryTest(BaseWatchTest):
    watch_kwargs = dict(
        key="history-test", label="History test", query="MacBook Pro",
        matches=lambda o: True, needs_config=True,
    )

    def test_standings_link_to_previous_low_for_same_model(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        previous_seen = now - timedelta(days=5)
        current = rw.Offer(P_M2, price=30_355, chip="M2 Max", ram_gb=64, ssd_gb=1000)
        key = rw.price_history_key(current)
        previous_url = "https://www.refurbed.se/p/apple-macbook-pro-2023-m2-14/old123/"
        self.state["price_lows"] = {
            key: {"price": 22_100, "url": previous_url,
                  "seen_at": previous_seen.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "screenshot": "price-proofs/2026-08-18/22100kr-proof.png",
                  "proof_validation": {"version": 1}}
        }
        site = FakeSite(
            [(P_M2, "30 355 kr")],
            titles={P_M2: ('<title>Apple MacBook Pro 2023 Apple M2 Max 12 Core '
                           '64.0 GB 1000 GB – refurbed</title>')},
        )

        with mock.patch.object(rw.time, "time", return_value=now.timestamp()):
            lines = self.run_once(site)

        item = next(line for line in lines if line.startswith("__ITEM__"))
        self.assertIn("Previous low for same model", item)
        self.assertIn("[22 100 kr]", item)
        self.assertIn(
            "github.com/moonlitnayeem/refurbed-mac-watch/blob/main/"
            "price-proofs/2026-08-18/22100kr-proof.png",
            item,
        )
        self.assertNotIn(previous_url, item)
        self.assertIn("5 days ago", item)

    def test_pre_screenshot_low_is_plain_text_not_a_mutable_product_link(self):
        markdown = rw.historical_low_markdown({
            "price": 22_100,
            "url": "https://www.refurbed.se/p/apple-macbook-pro/changed/",
            "seen_at": "2026-08-18T12:00:00Z",
        })

        self.assertEqual(markdown, "22 100 kr")
        self.assertNotIn("http", markdown)

    def test_unvalidated_screenshot_is_not_presented_as_proof(self):
        markdown = rw.historical_low_markdown({
            "price": 10_899,
            "screenshot": "price-proofs/2026-08-25/wrong.png",
        })

        self.assertEqual(markdown, "10 899 kr")

    def test_family_product_path_has_the_same_historical_identity_as_a_variant(self):
        family = rw.Offer(
            P_M1_ULTRA_FAMILY,
            chip="M1 Ultra",
            ram_gb=128,
            ssd_gb=2000,
        )
        variant = rw.Offer(
            P_M1_ULTRA_VARIANT,
            chip="M1 Ultra",
            ram_gb=128,
            ssd_gb=2000,
        )

        self.assertEqual(rw.price_history_key(family), rw.price_history_key(variant))
        self.assertEqual(
            rw.price_history_key(family),
            "apple-mac-studio-2022-m1-ultra|M1 Ultra|128|2000",
        )

    def test_price_history_update_keeps_all_time_lowest_observation(self):
        old = rw.Offer(P_M2, price=22_100, chip="M2 Max", ram_gb=64, ssd_gb=1000)
        key = rw.price_history_key(old)
        state = {"price_lows": {
            key: {"price": 22_100, "url": old.url,
                  "seen_at": "2026-08-18T12:00:00Z",
                  "screenshot": "price-proofs/2026-08-18/22100kr-existing.png"}
        }, "watches": {"x": {"offers": {
            P_M2: {"price": 30_355, "chip": "M2 Max", "ram_gb": 64,
                   "ssd_gb": 1000, "matched": True}
        }}}}

        requests = rw.update_price_lows(state, "2026-08-23T12:00:00Z")

        self.assertEqual(requests, [])
        self.assertEqual(state["price_lows"][key]["price"], 22_100)
        self.assertEqual(state["price_lows"][key]["seen_at"], "2026-08-18T12:00:00Z")
        self.assertIn("screenshot", state["price_lows"][key])

    def test_new_all_time_low_queues_immutable_screenshot_proof(self):
        key = rw.price_history_key(
            rw.Offer(P_M2, chip="M2 Max", ram_gb=64, ssd_gb=1000)
        )
        state = {
            "price_lows": {
                key: {"price": 22_100, "url": "https://example.com/old",
                      "seen_at": "2026-08-18T12:00:00Z",
                      "screenshot": "price-proofs/2026-08-18/old.png"}
            },
            "watches": {"x": {"offers": {
                P_M2: {"price": 21_900, "chip": "M2 Max", "ram_gb": 64,
                       "ssd_gb": 1000, "matched": True}
            }}},
        }

        requests = rw.update_price_lows(state, "2026-08-25T10:30:00Z")

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["key"], key)
        self.assertEqual(request["price"], 21_900)
        self.assertEqual(request["url"], rw.BASE + P_M2)
        self.assertEqual(request["previous"]["price"], 22_100)
        self.assertTrue(request["screenshot"].startswith("price-proofs/2026-08-25/"))
        self.assertTrue(request["screenshot"].endswith(".png"))
        self.assertEqual(state["price_lows"][key]["price"], 21_900)
        self.assertNotIn("screenshot", state["price_lows"][key])

    def test_one_run_queues_only_the_cheapest_observation_per_model(self):
        other_path = "/p/apple-macbook-pro-2023-m2-14/other/"
        fields = {"chip": "M2 Max", "ram_gb": 64, "ssd_gb": 1000,
                  "matched": True}
        key = rw.price_history_key(rw.Offer(P_M2, **{
            "chip": "M2 Max", "ram_gb": 64, "ssd_gb": 1000
        }))
        state = {
            "price_lows": {key: {"price": 25_000, "url": "https://example.com/old",
                                  "seen_at": "2026-08-18T12:00:00Z"}},
            "watches": {
                "first": {"offers": {P_M2: {**fields, "price": 24_000}}},
                "second": {"offers": {other_path: {**fields, "price": 23_000}}},
            },
        }

        requests = rw.update_price_lows(state, "2026-08-25T10:30:00Z")

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["price"], 23_000)
        self.assertTrue(requests[0]["url"].endswith(other_path))
        self.assertEqual(state["price_lows"][key]["price"], 23_000)

    def test_unknown_direct_page_observation_cannot_create_a_historical_low(self):
        state = {"price_lows": {}, "watches": {"mac-studio": {"offers": {
            P_M1_ULTRA_FAMILY: {
                "price": 32_499,
                "chip": "M1 Ultra",
                "ram_gb": 128,
                "ssd_gb": 2000,
                "matched": True,
                "verification_unknown": True,
            }
        }}}}

        requests = rw.update_price_lows(
            state,
            "2026-08-26T17:00:00Z",
            watch_keys=["mac-studio"],
        )

        self.assertEqual(requests, [])
        self.assertEqual(state["price_lows"], {})

    def test_stale_removed_watch_cannot_create_a_false_low(self):
        fields = {"chip": "M1 Max", "ram_gb": 64, "ssd_gb": 512,
                  "matched": True}
        path = "/p/apple-macbook-pro-2021-m1-16-2/179581b/"
        state = {"price_lows": {}, "watches": {
            "current": {"offers": {path: {**fields, "price": 21_019}}},
            "removed-old-watch": {"offers": {path: {**fields, "price": 21_009}}},
        }}

        requests = rw.update_price_lows(
            state, "2026-08-26T11:50:00Z", watch_keys=["current"]
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["price"], 21_019)

    def test_price_proof_manifest_is_written_and_stale_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.json"
            requests = [{"key": "model", "price": 21_900}]

            rw.write_price_proof_requests(requests, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), requests)

            rw.write_price_proof_requests([], path)
            self.assertFalse(path.exists())

    def test_archive_backfill_finds_lowest_price_and_timestamp(self):
        markdown = '''
        <summary>Update — 2026-08-18 12:00 UTC</summary>
        - [MacBook Pro · M2 Max · 64 GB · 1000 GB SSD](https://www.refurbed.se/p/apple-macbook-pro-2023-m2-14/a1/) — 22 100 kr
        <summary>Update — 2026-08-19 12:00 UTC</summary>
        - [MacBook Pro · M2 Max · 64 GB · 1000 GB SSD](https://www.refurbed.se/p/apple-macbook-pro-2023-m2-14/a2/) — 30 355 kr
        '''

        lows = rw.parse_archived_price_lows(markdown)
        key = "apple-macbook-pro-2023-m2-14|M2 Max|64|1000"

        self.assertEqual(lows[key]["price"], 22_100)
        self.assertTrue(lows[key]["url"].endswith("/a1/"))
        self.assertEqual(lows[key]["seen_at"], "2026-08-18T12:00:00Z")


class BuyNowSectionTest(unittest.TestCase):
    def _state(self, offers, low=22_100):
        sample = rw.Offer(P_M2, chip="M2 Max", ram_gb=64, ssd_gb=1000)
        return {
            "price_lows": {
                rw.price_history_key(sample): {
                    "price": low,
                    "url": "https://www.refurbed.se/p/apple-macbook-pro-2023-m2-14/old123/",
                    "seen_at": "2026-08-18T12:00:00Z",
                    "screenshot": "price-proofs/2026-08-18/22100kr-proof.png",
                    "proof_validation": {"version": 1},
                }
            },
            "watches": {"x": {"offers": offers}},
        }

    def test_near_low_section_deduplicates_models_and_uses_cheapest_available_variant(self):
        cheaper = "/p/apple-macbook-pro-2023-m2-14/cheap1/"
        other = "/p/apple-macbook-pro-2023-m2-14/other2/"
        fields = {"chip": "M2 Max", "ram_gb": 64, "ssd_gb": 1000,
                  "matched": True}
        state = self._state({
            other: {**fields, "price": 22_900},
            cheaper: {**fields, "price": 22_800},
            "/p/apple-macbook-pro-2023-m2-14/gone3/": {
                **fields, "price": 21_000, "matched": False,
            },
        })

        lines = rw.build_buy_now_lines(state, tolerance_kr=1_000)

        self.assertTrue(
            lines[0].startswith("__STANDINGS__🔔 At or Near Historical Lows Right Now")
        )
        items = [line for line in lines if line.startswith("__ITEM__[")]
        self.assertEqual(len(items), 1)
        self.assertIn(cheaper, items[0])
        self.assertIn("700 kr above historical low", items[0])
        self.assertIn("price-proofs/2026-08-18/22100kr-proof.png", items[0])
        self.assertNotIn("old123", items[0])

    def test_below_historical_low_stays_in_section(self):
        fields = {"chip": "M2 Max", "ram_gb": 64, "ssd_gb": 1000,
                  "matched": True, "price": 21_500}
        state = self._state({P_M2: fields})

        lines = rw.build_buy_now_lines(state, tolerance_kr=1_000)

        self.assertIn("600 kr below historical low", "\n".join(lines))

    def test_more_than_tolerance_above_low_is_excluded(self):
        fields = {"chip": "M2 Max", "ram_gb": 64, "ssd_gb": 1000,
                  "matched": True, "price": 23_101}
        state = self._state({P_M2: fields})

        lines = rw.build_buy_now_lines(state, tolerance_kr=1_000)

        self.assertIn("(0)", lines[0])
        self.assertIn("none currently available", lines[1])

    def test_unknown_direct_page_observation_is_not_a_buy_now_offer(self):
        offer = rw.Offer(
            P_M1_ULTRA_FAMILY,
            price=32_499,
            chip="M1 Ultra",
            ram_gb=128,
            ssd_gb=2000,
        )
        key = rw.price_history_key(offer)
        state = {
            "price_lows": {
                key: {
                    "price": 32_499,
                    "seen_at": "2026-08-26T17:00:00Z",
                }
            },
            "watches": {"mac-studio": {"offers": {
                P_M1_ULTRA_FAMILY: {
                    "price": 32_499,
                    "chip": "M1 Ultra",
                    "ram_gb": 128,
                    "ssd_gb": 2000,
                    "matched": True,
                    "verification_unknown": True,
                }
            }}},
        }

        lines = rw.build_buy_now_lines(state, tolerance_kr=1_000)

        self.assertIn("(0)", lines[0])
        self.assertIn("none currently available", lines[1])

    def test_buy_now_section_renders_before_existing_categories(self):
        lines = [
            "__STANDINGS__🔔 At or Near Historical Lows Right Now (0)",
            "__ITEM___none currently available_",
            "__STANDINGS__Best-value Macs (1)",
            "__ITEM__[Mac](https://example.com) — 1 kr",
        ]

        _, standings = rw.build_report(lines)

        self.assertLess(
            standings.index("🔔 At or Near Historical Lows Right Now"),
            standings.index("Best-value Macs"),
        )


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
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(rw.PHONE_ALERTS[0]["price"], 54_900)
        self.assertTrue(rw.PHONE_ALERTS[0]["url"].endswith(P_ULTRA))

    def test_first_run_baseline_does_not_send_phone_alert(self):
        site = FakeSite([(P_STUDIO, "19 845 kr")])

        self.run_once(site)

        self.assertEqual(rw.PHONE_ALERTS, [])

    def test_supplied_m1_ultra_family_url_is_listed_and_alerted_when_buyable(self):
        self.watch.direct_product_paths = (P_M1_ULTRA_FAMILY,)
        site = FakeSite(
            [(P_STUDIO, "19 845 kr")],
            titles={
                P_STUDIO: STUDIO_M1_TITLE,
                P_M1_ULTRA_FAMILY: STUDIO_M1_ULTRA_OUT_OF_STOCK,
            },
        )
        self.run_once(site)
        self.assertEqual(rw.PHONE_ALERTS, [], "out-of-stock baseline must stay silent")

        site.titles[P_M1_ULTRA_FAMILY] = (
            STUDIO_M1_ULTRA_TITLE
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )
        lines = self.run_once(site)

        item = next(line for line in lines if P_M1_ULTRA_FAMILY in line)
        self.assertIn("M1 Ultra · 128 GB · 2000 GB SSD", item)
        self.assertIn("32 499 kr", item)
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "Apple Silicon Ultra Mac Studio available",
        )
        self.assertEqual(rw.PHONE_ALERTS[0]["url"], rw.BASE + P_M1_ULTRA_FAMILY)

    def test_ultra_appearing_after_zero_offer_baseline_still_alerts(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK

        def fetch(url):
            if url in {
                rw.search_url("Mac Studio"),
                rw.search_url("Apple Mac Studio Ultra"),
            }:
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = (
                STUDIO_M1_ULTRA_TITLE
                + '<p data-test="product-price"><span>32 499 kr</span></p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "Apple Silicon Ultra Mac Studio available",
        )

    def test_direct_out_of_stock_preserves_distinct_same_family_variant_as_unknown(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        variant_available = True
        direct_page = (
            STUDIO_M1_ULTRA_TITLE
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_M1_ULTRA_VARIANT, "29 999 kr")] if variant_available else []
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_M1_ULTRA_VARIANT):
                return STUDIO_M1_ULTRA_64_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            variant_available = False
            direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            variant_available = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "canonical sold-out state must not clear a distinct same-family variant",
        )

    def test_explicit_direct_out_of_stock_survives_zero_search_and_restock_alerts(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_page = (
            STUDIO_M1_ULTRA_TITLE
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )

        def fetch(url):
            if url in {
                rw.search_url("Mac Studio"),
                rw.search_url("Apple Mac Studio Ultra"),
            }:
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = (
                STUDIO_M1_ULTRA_TITLE
                + '<p data-test="product-price"><span>32 499 kr</span></p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "Apple Silicon Ultra Mac Studio available",
        )

    def test_zero_search_with_direct_out_of_stock_preserves_other_offers_as_unknown(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        search_entries = [(P_STUDIO, "19 845 kr")]
        direct_page = (
            STUDIO_M1_ULTRA_TITLE
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html(search_entries)
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            search_entries = []
            direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            search_entries = [(P_STUDIO, "19 845 kr")]
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "a zero search must not turn unrelated preserved offers into fake restocks",
        )

    def test_broad_ultra_query_finds_and_alerts_any_ultra_generation(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        ultra_available = False
        requested = []

        def fetch(url):
            nonlocal ultra_available
            requested.append(url)
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_ULTRA, "54 900 kr")] if ultra_available else []
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            if url.endswith(P_ULTRA):
                return STUDIO_ULTRA_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_available = True
            lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )

        self.assertIn(rw.search_url("Apple Mac Studio Ultra"), requested)
        self.assertIn(P_ULTRA, "\n".join(lines))
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "Apple Silicon Ultra Mac Studio available",
        )

    def test_unknown_canonical_preserves_its_persisted_exact_alias(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        phase = "baseline"

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = (
                    [(P_M1_ULTRA_VARIANT, "32 499 kr")]
                    if phase == "baseline" else []
                )
                return search_html(entries)
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            if url.endswith(P_M1_ULTRA_VARIANT):
                return STUDIO_M1_ULTRA_TITLE
            if url.endswith(P_M1_ULTRA_FAMILY):
                if phase == "unknown":
                    return (
                        '<title>Apple Mac Studio 2022 Apple M2 Ultra 20 Core '
                        '128.0 GB 2000 GB – refurbed</title>'
                        '<p>Produkten finns för närvarande inte i lager</p>'
                    )
                return (
                    STUDIO_M1_ULTRA_TITLE
                    + '<p data-test="product-price"><span>32 499 kr</span></p>'
                )
            return None

        # Start from an initialized empty watch so baseline availability can alert;
        # then clear it to observe only false recovery alerts.
        self.state["watches"]["mac-studio"] = {"offers": {}}
        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            baseline = self.state["watches"]["mac-studio"]["offers"]
            self.assertIn(P_M1_ULTRA_VARIANT, baseline)
            self.assertNotIn(P_M1_ULTRA_FAMILY, baseline)
            rw.PHONE_ALERTS.clear()

            phase = "unknown"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            unknown = self.state["watches"]["mac-studio"]["offers"]
            self.assertIn(P_M1_ULTRA_VARIANT, unknown)
            self.assertTrue(
                unknown[P_M1_ULTRA_VARIANT].get("verification_unknown")
            )

            phase = "recovered"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "canonical recovery must reuse its exact alias rather than false-restock",
        )

    def test_explicit_out_of_stock_overrides_stale_direct_price(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        stale_sold_out_page = (
            STUDIO_M1_ULTRA_OUT_OF_STOCK
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )

        def fetch(url):
            if url in {
                rw.search_url("Mac Studio"),
                rw.search_url("Apple Mac Studio Ultra"),
            }:
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return stale_sold_out_page
            return None

        # An existing empty watch means a false direct match would alert now.
        self.state["watches"]["mac-studio"] = {"offers": {}}
        with mock.patch.object(rw, "fetch", side_effect=fetch):
            lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )

        self.assertEqual(rw.PHONE_ALERTS, [])
        self.assertNotIn(P_M1_ULTRA_FAMILY, "\n".join(lines))
        self.assertNotIn(
            P_M1_ULTRA_FAMILY,
            self.state["watches"]["mac-studio"]["offers"],
        )

    def test_zero_search_with_available_direct_ultra_preserves_other_offers(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        studio_in_search = True
        direct_available = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")] if studio_in_search else [])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            if url.endswith(P_M1_ULTRA_FAMILY):
                if direct_available:
                    return (
                        STUDIO_M1_ULTRA_TITLE
                        + '<p data-test="product-price"><span>32 499 kr</span></p>'
                    )
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            rw.PHONE_ALERTS.clear()

            studio_in_search = False
            direct_available = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            self.assertEqual(
                [alert.get("title") for alert in rw.PHONE_ALERTS],
                ["Apple Silicon Ultra Mac Studio available"],
            )
            preserved = self.state["watches"]["mac-studio"]["offers"][P_STUDIO]
            self.assertTrue(preserved.get("verification_unknown"))

            studio_in_search = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            len(rw.PHONE_ALERTS),
            1,
            "search recovery must not falsely re-alert the continuously available M1 Max",
        )

    def test_primary_search_failure_still_polls_direct_ultra_product(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_available = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return None
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                if direct_available:
                    return (
                        STUDIO_M1_ULTRA_TITLE
                        + '<p data-test="product-price"><span>32 499 kr</span></p>'
                    )
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_available = True
            rw.PHONE_ALERTS.clear()
            lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertTrue(rw.PHONE_ALERTS[0]["url"].endswith(P_M1_ULTRA_FAMILY))
        self.assertTrue(any(P_M1_ULTRA_FAMILY in line for line in lines))

    def test_failed_ultra_query_does_not_realert_unchanged_offer_on_recovery(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        ultra_query_fails = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                if ultra_query_fails:
                    return None
                return search_html([(P_ULTRA, "54 900 kr")])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            if url.endswith(P_ULTRA):
                return STUDIO_ULTRA_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_query_fails = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_query_fails = False
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "a transient Ultra-query failure must not manufacture a reappearance",
        )

    def test_query_failure_source_switch_does_not_duplicate_price_drop_on_recovery(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        ultra_query_fails = False
        variant_price = "32 499 kr"
        direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                if ultra_query_fails:
                    return None
                return search_html([(P_M1_ULTRA_VARIANT, variant_price)])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            if url.endswith(P_M1_ULTRA_VARIANT):
                return STUDIO_M1_ULTRA_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch), \
             mock.patch.object(
                 rw,
                 "notify",
                 side_effect=lambda t, s, m, url=None: self.notes.append((t, s, m, url)),
             ):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_query_fails = True
            direct_page = (
                STUDIO_M1_ULTRA_TITLE
                + '<p data-test="product-price"><span>29 999 kr</span></p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_query_fails = False
            variant_price = "29 999 kr"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(self.notes), 1)
        self.assertIn("32 499 kr → 29 999 kr", self.notes[0][2])

    def test_unverified_ultra_search_card_cannot_consume_dedicated_alert(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        ultra_available = False
        ultra_page_verified = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_ULTRA, "54 900 kr")] if ultra_available else []
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            if url.endswith(P_ULTRA):
                return STUDIO_ULTRA_TITLE if ultra_page_verified else None
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_available = True
            unverified_lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )
            ultra_page_verified = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertNotIn(P_ULTRA, "\n".join(unverified_lines))
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "Apple Silicon Ultra Mac Studio available",
        )

    def test_dedicated_ultra_query_requires_verification_when_slug_is_generic(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        ultra_available = False
        ultra_page_verified = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_ULTRA_GENERIC_SLUG, "54 900 kr")] if ultra_available else []
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            if url.endswith(P_ULTRA_GENERIC_SLUG):
                return STUDIO_ULTRA_TITLE if ultra_page_verified else None
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_available = True
            unverified_lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )
            ultra_page_verified = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertNotIn(P_ULTRA_GENERIC_SLUG, "\n".join(unverified_lines))
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "Apple Silicon Ultra Mac Studio available",
        )

    def test_direct_and_search_discovery_keep_distinct_same_family_configurations(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        available = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_M1_ULTRA_VARIANT, "29 999 kr")] if available else []
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                if not available:
                    return STUDIO_M1_ULTRA_OUT_OF_STOCK
                return (
                    STUDIO_M1_ULTRA_TITLE
                    + '<p data-test="product-price"><span>32 499 kr</span></p>'
                )
            if url.endswith(P_M1_ULTRA_VARIANT):
                return STUDIO_M1_ULTRA_64_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            available = True
            lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )

        items = [line for line in lines if line.startswith("__ITEM__")]
        self.assertEqual(len(items), 2)
        self.assertTrue(any("64 GB · 1000 GB SSD" in line for line in items))
        self.assertTrue(any("128 GB · 2000 GB SSD" in line for line in items))
        self.assertEqual(len(rw.PHONE_ALERTS), 2)

    def test_direct_and_search_discovery_do_not_duplicate_same_ultra_family(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        ultra_available = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_M1_ULTRA_VARIANT, "32 499 kr")] if ultra_available else []
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                if ultra_available:
                    return (
                        STUDIO_M1_ULTRA_TITLE
                        + '<p data-test="product-price"><span>32 499 kr</span></p>'
                    )
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            if url.endswith(P_M1_ULTRA_VARIANT):
                return STUDIO_M1_ULTRA_TITLE
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            ultra_available = True
            lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )

        ultra_items = [
            line for line in lines
            if line.startswith("__ITEM__") and "M1 Ultra" in line
        ]
        self.assertEqual(len(ultra_items), 1)
        self.assertIn(P_M1_ULTRA_VARIANT, ultra_items[0])
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertTrue(rw.PHONE_ALERTS[0]["url"].endswith(P_M1_ULTRA_VARIANT))

    def test_canonical_to_variant_source_switch_does_not_duplicate_alert(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        variant_in_search = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_M1_ULTRA_VARIANT, "32 499 kr")] if variant_in_search else []
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                return (
                    STUDIO_M1_ULTRA_TITLE
                    + '<p data-test="product-price"><span>32 499 kr</span></p>'
                )
            if url.endswith(P_M1_ULTRA_VARIANT):
                return STUDIO_M1_ULTRA_TITLE
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            variant_in_search = True
            lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )

        ultra_items = [
            line for line in lines
            if line.startswith("__ITEM__") and "M1 Ultra" in line
        ]
        self.assertEqual(len(ultra_items), 1)
        self.assertIn(P_M1_ULTRA_VARIANT, ultra_items[0])
        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "switching from canonical to exact variant is not a new availability",
        )

    def test_direct_failure_during_source_switch_does_not_hide_later_restock(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        variant_in_search = False
        extra_query_fails = False
        direct_page = (
            STUDIO_M1_ULTRA_TITLE
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                entries = [(P_M1_ULTRA_VARIANT, "32 499 kr")] if variant_in_search else []
                return search_html(entries)
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return None if extra_query_fails else search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_M1_ULTRA_VARIANT):
                return STUDIO_M1_ULTRA_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            variant_in_search = True
            extra_query_fails = True
            direct_page = None
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            variant_in_search = False
            extra_query_fails = False
            direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            variant_in_search = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertTrue(rw.PHONE_ALERTS[0]["url"].endswith(P_M1_ULTRA_VARIANT))

    def test_one_canonical_alias_can_suppress_only_one_new_exact_variant(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        variants_in_search = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = []
                if variants_in_search:
                    entries = [
                        (P_M1_ULTRA_VARIANT, "32 499 kr"),
                        (P_M1_ULTRA_VARIANT_B, "31 999 kr"),
                    ]
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                return (
                    STUDIO_M1_ULTRA_TITLE
                    + '<p data-test="product-price"><span>32 499 kr</span></p>'
                )
            if url.endswith(P_M1_ULTRA_VARIANT) or url.endswith(P_M1_ULTRA_VARIANT_B):
                return STUDIO_M1_ULTRA_TITLE
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            variants_in_search = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertTrue(rw.PHONE_ALERTS[0]["url"].endswith(P_M1_ULTRA_VARIANT_B))

    def test_second_exact_variant_in_same_ultra_family_still_alerts(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        include_second_variant = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                entries = [(P_M1_ULTRA_VARIANT, "32 499 kr")]
                if include_second_variant:
                    entries.append((P_M1_ULTRA_VARIANT_B, "31 999 kr"))
                return search_html(entries)
            if url.endswith(P_M1_ULTRA_FAMILY):
                return STUDIO_M1_ULTRA_OUT_OF_STOCK
            if url.endswith(P_M1_ULTRA_VARIANT) or url.endswith(P_M1_ULTRA_VARIANT_B):
                return STUDIO_M1_ULTRA_TITLE
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            include_second_variant = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertTrue(rw.PHONE_ALERTS[0]["url"].endswith(P_M1_ULTRA_VARIANT_B))

    def test_transient_direct_page_failure_does_not_create_false_restock_alert(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_fetch_fails = False

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                if direct_fetch_fails:
                    return None
                return (
                    STUDIO_M1_ULTRA_TITLE
                    + '<p data-test="product-price"><span>32 499 kr</span></p>'
                )
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_fetch_fails = True
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_fetch_fails = False
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "an unknown fetch must preserve last-known state instead of faking a restock",
        )

    def test_incomplete_direct_page_is_unknown_not_out_of_stock(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_page = (
            STUDIO_M1_ULTRA_TITLE
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = "<html><title>Temporary incomplete page</title></html>"
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = (
                STUDIO_M1_ULTRA_TITLE
                + '<p data-test="product-price"><span>32 499 kr</span></p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "missing price and missing out-of-stock marker is an unknown page",
        )

    def test_unverified_price_page_cannot_consume_the_real_ultra_alert(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = (
                "<html><title>Temporary incomplete page</title>"
                '<p data-test="product-price"><span>32 499 kr</span></p></html>'
            )
            incomplete_lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )
            direct_page = (
                STUDIO_M1_ULTRA_TITLE
                + '<p data-test="product-price"><span>32 499 kr</span></p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertNotIn(P_M1_ULTRA_FAMILY, "\n".join(incomplete_lines))
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(
            rw.PHONE_ALERTS[0]["title"],
            "Apple Silicon Ultra Mac Studio available",
        )

    def test_wrong_generation_out_of_stock_page_is_unknown(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_page = (
            STUDIO_M1_ULTRA_TITLE
            + '<p data-test="product-price"><span>32 499 kr</span></p>'
        )

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = (
                '<title>Apple Mac Studio 2022 Apple M2 Ultra 20 Core '
                '128.0 GB 2000 GB – refurbed</title>'
                '<p>Produkten finns för närvarande inte i lager</p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = (
                STUDIO_M1_ULTRA_TITLE
                + '<p data-test="product-price"><span>32 499 kr</span></p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertEqual(
            rw.PHONE_ALERTS,
            [],
            "a wrong-generation sold-out page must not manufacture an M1 restock",
        )

    def test_wrong_ultra_generation_at_m1_family_url_is_rejected(self):
        self.watch = next(w for w in rw.WATCHES if w.key == "mac-studio")
        direct_page = STUDIO_M1_ULTRA_OUT_OF_STOCK

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([(P_STUDIO, "19 845 kr")])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return direct_page
            if url.endswith(P_STUDIO):
                return STUDIO_M1_TITLE
            return None

        with mock.patch.object(rw, "fetch", side_effect=fetch):
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])
            direct_page = (
                '<title>Apple Mac Studio 2022 Apple M2 Ultra 20 Core '
                '128.0 GB 2000 GB – refurbed</title>'
                '<p data-test="product-price"><span>32 499 kr</span></p>'
            )
            wrong_lines = rw.run_watch(
                self.watch, self.state, dry_run=False, notifications_left=[8]
            )
            direct_page = (
                STUDIO_M1_ULTRA_TITLE
                + '<p data-test="product-price"><span>32 499 kr</span></p>'
            )
            rw.run_watch(self.watch, self.state, dry_run=False, notifications_left=[8])

        self.assertNotIn(P_M1_ULTRA_FAMILY, "\n".join(wrong_lines))
        self.assertEqual(len(rw.PHONE_ALERTS), 1)
        self.assertEqual(rw.PHONE_ALERTS[0]["model"].split(" ·")[0], "M1 Ultra")

    def test_first_studio_after_empty_period_notifies(self):
        """Today's real state: zero available. One appearing is the whole point."""
        site = FakeSite([(P_MINI, "11 615 kr")])
        self.run_once(site)
        self.notes.clear()
        site.entries = [(P_MINI, "11 615 kr"), (P_STUDIO, "19 845 kr")]
        self.run_once(site)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("M1 Max", self.notes[0][1])


class ListModeTest(unittest.TestCase):
    def test_list_mode_includes_extra_query_and_direct_product_matches(self):
        watch = next(w for w in rw.WATCHES if w.key == "mac-studio")

        def fetch(url):
            if url == rw.search_url("Mac Studio"):
                return search_html([])
            if url == rw.search_url("Apple Mac Studio Ultra"):
                return search_html([(P_ULTRA, "54 900 kr")])
            if url.endswith(P_M1_ULTRA_FAMILY):
                return (
                    STUDIO_M1_ULTRA_TITLE
                    + '<p data-test="product-price"><span>32 499 kr</span></p>'
                )
            if url.endswith(P_ULTRA):
                return STUDIO_ULTRA_TITLE
            return None

        stdout = io.StringIO()
        with mock.patch.object(rw, "WATCHES", (watch,)), \
             mock.patch.object(rw, "fetch", side_effect=fetch), \
             mock.patch.object(rw.logging, "basicConfig"), \
             mock.patch.object(rw, "NOTIFY_BACKEND", rw.NOTIFY_BACKEND), \
             mock.patch("sys.argv", ["refurbed_watch.py", "--list", "--notify", "none"]), \
             mock.patch("sys.stdout", stdout):
            result = rw.main()

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn(P_M1_ULTRA_FAMILY, output)
        self.assertIn(P_ULTRA, output)
        self.assertIn("M1 Ultra · 128 GB · 2000 GB SSD", output)
        self.assertIn("M3 Ultra · 256 GB · 1000 GB SSD", output)


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
        self.assertIn("View older updates", body)
        self.assertIn("/tree/main/history", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
