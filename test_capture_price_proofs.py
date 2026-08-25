#!/usr/bin/env python3

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

import capture_price_proofs as cpp


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
            chrome = root / "fake-chrome.py"
            chrome.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import binascii
                import os
                import struct
                import sys
                import zlib
                from pathlib import Path

                output = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--screenshot="))
                width, height = 1440, 1200
                raw = b"".join(b"\\x00" + os.urandom(width * 3) for _ in range(height))

                def chunk(kind, data):
                    return (struct.pack(">I", len(data)) + kind + data
                            + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff))

                png = (b"\\x89PNG\\r\\n\\x1a\\n"
                       + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                       + chunk(b"IDAT", zlib.compress(raw))
                       + chunk(b"IEND", b""))
                Path(output).write_bytes(png)
            """), encoding="utf-8")
            chrome.chmod(0o755)

            captured = cpp.capture_all(
                state_path=state_path,
                requests_path=requests_path,
                root=root,
                chrome_bin=str(chrome),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
