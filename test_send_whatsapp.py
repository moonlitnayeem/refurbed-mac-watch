#!/usr/bin/env python3

import unittest

import send_whatsapp as sw


class WhatsAppPayloadTest(unittest.TestCase):
    def test_mac_studio_template_payload(self):
        alert = {
            "model": "M3 Ultra · 256 GB · 1000 GB SSD",
            "price": 54_900,
            "url": "https://www.refurbed.se/p/apple-mac-studio-2025-m3-ultra/99001a/",
        }

        payload = sw.template_payload(
            alert, to="46737867931", template_name="mac_studio_alert"
        )

        self.assertEqual(payload["to"], "46737867931")
        self.assertEqual(payload["template"]["name"], "mac_studio_alert")
        params = payload["template"]["components"][0]["parameters"]
        self.assertEqual([item["text"] for item in params], [
            "M3 Ultra · 256 GB · 1000 GB SSD",
            "54 900 kr",
            alert["url"],
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
