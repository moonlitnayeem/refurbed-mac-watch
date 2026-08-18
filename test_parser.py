#!/usr/bin/env python3
"""
Tests for refurbed_watch parsing.

The fixtures reproduce the markup shape observed on live refurbed.se pages on
2026-08-18, including the awkward bits:
  * a star rating ("4,8") rendered immediately before the price, whose trailing
    digit a naive price regex will swallow -> "8 33 055 kr"
  * thousands separated by a space ("33 055 kr")
  * a struck-through reference price with decimals ("35 589,35 kr")
  * plain product links with no variant segment, which must NOT be offers
  * an availability badge that may or may not be present

The <title> strings are verbatim from live variant pages.
"""

import unittest

from refurbed_watch import (
    Offer,
    is_mac_studio,
    is_max_or_ultra_64gb,
    parse_search_page,
    parse_variant_title,
)


def card(href, alt, badge, price_txt, was_txt, rating="4,8"):
    badge_html = f'<span class="badge">{badge}</span>' if badge else ""
    return f'''
<article class="group flex flex-col">
  <a href="{href}" class="block">
    {badge_html}
    <img src="https://files.refurbed.com/ii/x.jpg" alt="{alt}"/>
    <h2 class="title">{alt}&quot;</h2>
    <div class="rating"><span>{rating}</span></div>
    <div class="price">
      <span class="now">{price_txt}</span>
      <span class="was">{was_txt}</span>
      <span>(Nypris)</span>
    </div>
  </a>
</article>'''


SEARCH_FIXTURE = (
    '<html><body>'
    '<nav><a href="/p/apple-macbook-pro-2021-m1-14/">Category link, not an offer</a>'
    '<a href="/p/iphone-13/">iPhone 13</a></nav>'
    + card("/p/apple-macbook-pro-2021-m1-14/75706/",
           "Apple MacBook Pro 2021 M1 | 14.2",
           "Bästsäljare", "33 035 kr", "35 589,35 kr")
    + card("/p/apple-macbook-pro-2023-m3-16-2/258184c/",
           "Apple MacBook Pro 2023 M3 | 16.2",
           "Bara ett fåtal kvar", "39 499 kr", "57 192,05 kr", rating="4,7")
    + card("/p/apple-macbook-pro-2024-m4-14/282199c/",
           "Apple MacBook Pro 2024 M4 | 14",
           None, "36 405 kr", "41 871,77 kr", rating="4,5")
    + '</body></html>'
)

# The real "Mac Studio" search returns mostly non-Mac-Studio products.
MAC_STUDIO_FUZZY_FIXTURE = (
    '<html><body>'
    + card("/p/apple-mac-mini-2024-m4/273548aa/", "Apple Mac Mini 2024 M4",
           None, "11 615 kr", "15 000,00 kr")
    + card("/p/apple-studio-display-27-2026/423688aa/", "Apple Studio Display 2026",
           "Bara ett fåtal kvar", "17 281 kr", "20 000,00 kr")
    + card("/p/apple-mac-pro-rack-2019/478657/", "Apple Mac Pro Rack 2019",
           None, "18 715 kr", "25 000,00 kr")
    + '</body></html>'
)

TITLE_M3_MAX_64 = (
    '<title>Apple MacBook Pro 2023 M3 Apple M3 Max 16 Core 64.0 GB 2000 GB '
    '16.2 &quot; rymdsvart SE (Svensk) – refurbed</title>'
)
TITLE_M4_MAX_36 = (
    '<title>Apple MacBook Pro 2024 M4 Apple M4 Max 14 Core 36.0 GB 1000 GB '
    '14.2 &quot; silver SE (Svensk) – refurbed</title>'
)
TITLE_MAC_STUDIO = (
    '<title>Apple Mac Studio 2022 Apple M1 Max 10 Core 32.0 GB 512 GB '
    'silver SE (Svensk) – refurbed</title>'
)


class TestSearchParsing(unittest.TestCase):
    def test_finds_only_variant_cards(self):
        offers = parse_search_page(SEARCH_FIXTURE)
        self.assertEqual(len(offers), 3)
        self.assertEqual(
            [o.path for o in offers],
            ["/p/apple-macbook-pro-2021-m1-14/75706/",
             "/p/apple-macbook-pro-2023-m3-16-2/258184c/",
             "/p/apple-macbook-pro-2024-m4-14/282199c/"],
        )

    def test_category_links_are_not_offers(self):
        for o in parse_search_page(SEARCH_FIXTURE):
            self.assertRegex(o.path, r"^/p/[a-z0-9-]+/[0-9a-z]+/$")

    def test_price_ignores_the_rating_digit(self):
        """The regression this regex exists for: '4,8' must not become '8 33 035'."""
        offers = parse_search_page(SEARCH_FIXTURE)
        self.assertEqual([o.price for o in offers], [33035, 39499, 36405])

    def test_was_price_parsed_with_decimals(self):
        offers = parse_search_page(SEARCH_FIXTURE)
        self.assertEqual(offers[0].was_price, 35589)
        self.assertEqual(offers[1].was_price, 57192)

    def test_badges(self):
        offers = parse_search_page(SEARCH_FIXTURE)
        self.assertEqual(offers[0].badge, "Bästsäljare")
        self.assertEqual(offers[1].badge, "Bara ett fåtal kvar")
        self.assertIsNone(offers[2].badge)

    def test_name_from_alt(self):
        offers = parse_search_page(SEARCH_FIXTURE)
        self.assertEqual(offers[0].name, "Apple MacBook Pro 2021 M1 | 14.2")

    def test_empty_page_yields_nothing(self):
        self.assertEqual(parse_search_page("<html><body>Inga träffar</body></html>"), [])

    def test_duplicate_variant_deduped(self):
        dupe = SEARCH_FIXTURE + card("/p/apple-macbook-pro-2021-m1-14/75706/",
                                     "Apple MacBook Pro 2021 M1 | 14.2",
                                     None, "33 035 kr", "35 589,35 kr")
        self.assertEqual(len(parse_search_page(dupe)), 3)


class TestVariantTitleParsing(unittest.TestCase):
    def test_m3_max_64gb(self):
        config, chip, ram, ssd = parse_variant_title(TITLE_M3_MAX_64)
        self.assertEqual((chip, ram, ssd), ("M3 Max", 64.0, 2000.0))
        self.assertNotIn("refurbed", config)

    def test_m4_max_36gb(self):
        _, chip, ram, ssd = parse_variant_title(TITLE_M4_MAX_36)
        self.assertEqual((chip, ram, ssd), ("M4 Max", 36.0, 1000.0))

    def test_mac_studio_title(self):
        _, chip, ram, _ = parse_variant_title(TITLE_MAC_STUDIO)
        self.assertEqual((chip, ram), ("M1 Max", 32.0))

    def test_missing_title_is_safe(self):
        self.assertEqual(parse_variant_title("<html>no title</html>"), ("", "", None, None))


class TestMacStudioFilter(unittest.TestCase):
    """refurbed's 'Mac Studio' search is mostly not Mac Studios."""

    def test_rejects_every_fuzzy_match(self):
        offers = parse_search_page(MAC_STUDIO_FUZZY_FIXTURE)
        self.assertEqual(len(offers), 3)
        self.assertEqual([o for o in offers if is_mac_studio(o)], [])

    def test_studio_display_is_not_a_mac_studio(self):
        """Closest trap: the name contains 'Studio' but it is a monitor."""
        self.assertFalse(is_mac_studio(Offer(path="/p/apple-studio-display-27-2026/423688aa/")))

    def test_accepts_real_mac_studio(self):
        self.assertTrue(is_mac_studio(Offer(path="/p/apple-mac-studio-2022-m1-max/72461aa/")))
        self.assertTrue(is_mac_studio(Offer(path="/p/apple-mac-studio-2025-m3-ultra/99001a/")))


class TestMaxRamFilter(unittest.TestCase):
    def _offer(self, chip, ram):
        return Offer(path="/p/x/1/", chip=chip, ram_gb=ram)

    def test_accepts_max_with_64gb(self):
        self.assertTrue(is_max_or_ultra_64gb(self._offer("M3 Max", 64.0)))
        self.assertTrue(is_max_or_ultra_64gb(self._offer("M1 Max", 64.0)))

    def test_rejects_max_with_wrong_ram(self):
        """The real false positive: M4 Max / 36 GB is returned by this search."""
        self.assertFalse(is_max_or_ultra_64gb(self._offer("M4 Max", 36.0)))
        self.assertFalse(is_max_or_ultra_64gb(self._offer("M4 Max", 128.0)))

    def test_rejects_pro_chip(self):
        self.assertFalse(is_max_or_ultra_64gb(self._offer("M4 Pro", 48.0)))
        self.assertFalse(is_max_or_ultra_64gb(self._offer("M3 Pro", 64.0)))

    def test_rejects_unknown_config(self):
        self.assertFalse(is_max_or_ultra_64gb(self._offer("", None)))


class TestOfferLabel(unittest.TestCase):
    def test_prefers_verified_config_over_card_title(self):
        o = Offer(path="/p/x/1/", name="Apple MacBook Pro 2023 M3 | 16.2",
                  chip="M3 Max", ram_gb=64.0, ssd_gb=2000.0)
        self.assertEqual(o.label, "M3 Max · 64 GB · 2000 GB SSD")

    def test_falls_back_to_name(self):
        o = Offer(path="/p/x/1/", name="Apple Mac Studio")
        self.assertEqual(o.label, "Apple Mac Studio")


if __name__ == "__main__":
    unittest.main(verbosity=2)
