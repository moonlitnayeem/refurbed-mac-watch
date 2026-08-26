#!/usr/bin/env python3

import binascii
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import capture_price_proofs as cpp


def write_test_png(path, width=1440, height=1200):
    raw = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


class FakeBrowser:
    def __init__(self, html):
        self.html = html
        self.accepted_all = False
        self.captured_after_acceptance = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def open(self, url):
        self.url = url

    def accept_all_cookies(self):
        self.accepted_all = True

    def page_html(self):
        return self.html

    def screenshot(self, path):
        self.captured_after_acceptance = self.accepted_all
        write_test_png(path)


class PriceProofCaptureTest(unittest.TestCase):
    def test_rejects_non_refurbed_urls_and_paths_outside_proof_folder(self):
        valid = {
            "key": "model|M2 Max|64|1000",
            "price": 21_900,
            "url": "https://www.refurbed.se/p/apple-macbook-pro/x/",
            "seen_at": "2026-08-25T10:30:00Z",
            "screenshot": "price-proofs/2026-08-25/proof.png",
        }

        cpp.validate_request(valid)
        with self.assertRaisesRegex(ValueError, "Refurbed product URL"):
            cpp.validate_request({**valid, "url": "https://example.com/steal"})
        with self.assertRaisesRegex(ValueError, "price-proofs"):
            cpp.validate_request({**valid, "screenshot": "../outside.png"})

    def test_captures_half_page_png_and_attaches_it_to_matching_state(self):
        request = {
            "key": "apple-macbook-pro-2023-m2-14|M2 Max|64|1000",
            "price": 21_900,
            "url": "https://www.refurbed.se/p/apple-macbook-pro-2023-m2-14/x/",
            "seen_at": "2026-08-25T10:30:00Z",
            "screenshot": "price-proofs/2026-08-25/21900kr-proof.png",
        }
        state = {"price_lows": {request["key"]: {
            "price": request["price"],
            "url": request["url"],
            "seen_at": request["seen_at"],
        }}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            requests_path = root / "price_proof_requests.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            requests_path.write_text(json.dumps([request]), encoding="utf-8")
            browser = FakeBrowser(
                '<title>Apple MacBook Pro 2023 Apple M2 Max 64.0 GB '
                '1000 GB 14.2 " – refurbed</title>'
                '<p data-test="product-price"><span>21 900 kr</span></p>'
            )

            captured = cpp.capture_all(
                state_path=state_path,
                requests_path=requests_path,
                root=root,
                chrome_bin="unused-chrome",
                browser_factory=lambda _: browser,
            )

            self.assertEqual(captured, 1)
            screenshot = root / request["screenshot"]
            self.assertTrue(screenshot.exists())
            self.assertEqual(cpp.png_dimensions(screenshot), (1440, 1200))
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["price_lows"][request["key"]]["screenshot"],
                request["screenshot"],
            )
            self.assertTrue(browser.captured_after_acceptance)

    def test_accepts_all_cookies_before_capturing_the_page(self):
        request = {
            "key": "apple-macbook-pro-2021-m1-16-2|M1 Max|64|512",
            "price": 21_239,
            "url": "https://www.refurbed.se/p/apple-macbook-pro-2021-m1-16-2/179581b/",
            "seen_at": "2026-08-26T16:20:00Z",
            "screenshot": "price-proofs/2026-08-26/cookie-free.png",
        }
        html = (
            '<title>Apple MacBook Pro 2021 Apple M1 Max 64.0 GB 512 GB '
            '16.2 " – refurbed</title>'
            '<p data-test="product-price"><span>21 239 kr</span></p>'
        )

        browser = FakeBrowser(html)
        with tempfile.TemporaryDirectory() as tmp:
            target, validation = cpp._capture_one(
                request,
                Path(tmp),
                "unused-chrome",
                browser_factory=lambda _: browser,
            )

            self.assertTrue(browser.accepted_all)
            self.assertTrue(browser.captured_after_acceptance)
            self.assertTrue(target.exists())
            self.assertEqual(validation["price"], 21_239)

    def test_rejects_screenshot_when_rendered_configuration_does_not_match_request(self):
        request = {
            "key": "apple-macbook-pro-2021-m1-16-2|M1 Max|64|512",
            "price": 10_899,
            "url": "https://www.refurbed.se/p/apple-macbook-pro-2021-m1-16-2/wrong16/",
            "seen_at": "2026-08-25T16:21:28Z",
            "screenshot": "price-proofs/2026-08-25/wrong.png",
            "previous": None,
        }
        state = {"price_lows": {request["key"]: {
            "price": request["price"],
            "url": request["url"],
            "seen_at": request["seen_at"],
        }}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            requests_path = root / "price_proof_requests.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            requests_path.write_text(json.dumps([request]), encoding="utf-8")
            browser = FakeBrowser(
                '<title>Apple MacBook Pro 2021 Apple M1 Pro 16.0 GB '
                '512 GB 16.2 " – refurbed</title>'
                '<p data-test="product-price"><span>10 899 kr</span></p>'
            )

            captured = cpp.capture_all(
                state_path,
                requests_path,
                root,
                "unused-chrome",
                browser_factory=lambda _: browser,
            )

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(captured, 0)
            self.assertNotIn(request["key"], saved["price_lows"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
