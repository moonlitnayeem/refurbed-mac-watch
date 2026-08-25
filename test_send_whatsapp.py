#!/usr/bin/env python3

import unittest

import send_whatsapp as sw


class WhatsAppPayloadTest(unittest.TestCase):
    def setUp(self):
        self.alert = {
            "model": "M3 Ultra · 256 GB · 1000 GB SSD",
            "price": 54_900,
            "url": "https://www.refurbed.se/p/apple-mac-studio-2025-m3-ultra/99001a/",
        }

    def test_sandbox_freeform_payload(self):
        payload = sw.message_form(
            self.alert,
            to="+46700000000",
            sender="+14155238886",
        )

        self.assertEqual(payload["To"], "whatsapp:+46700000000")
        self.assertEqual(payload["From"], "whatsapp:+14155238886")
        self.assertEqual(
            payload["Body"],
            "Mac Studio available: M3 Ultra · 256 GB · 1000 GB SSD\n"
            "Price: 54 900 kr\n"
            "https://www.refurbed.se/p/apple-mac-studio-2025-m3-ultra/99001a/",
        )

    def test_approved_content_template_payload(self):
        payload = sw.message_form(
            self.alert,
            to="46700000000",
            sender="whatsapp:+14155238886",
            content_sid="HX123",
        )

        self.assertNotIn("Body", payload)
        self.assertEqual(payload["ContentSid"], "HX123")
        self.assertEqual(sw.json.loads(payload["ContentVariables"]), {
            "1": "M3 Ultra · 256 GB · 1000 GB SSD",
            "2": "54 900 kr",
            "3": self.alert["url"],
        })

    def test_twilio_trial_appointment_template_uses_two_variables(self):
        payload = sw.message_form(
            self.alert,
            to="46700000000",
            sender="+4915000000000",
            content_sid="HXTRIAL",
            template_style="appointment",
        )

        self.assertEqual(sw.json.loads(payload["ContentVariables"]), {
            "1": "Mac Studio available: M3 Ultra · 256 GB · 1000 GB SSD — 54 900 kr",
            "2": self.alert["url"],
        })

    def test_twilio_trial_static_template_has_no_variables(self):
        payload = sw.message_form(
            self.alert,
            to="46700000000",
            sender="+4915000000000",
            content_sid="HXSTATIC",
            template_style="static",
        )

        self.assertEqual(payload["ContentSid"], "HXSTATIC")
        self.assertNotIn("ContentVariables", payload)

    def test_api_key_credentials_are_preferred(self):
        self.assertEqual(
            sw.auth_credentials(
                "AC123",
                auth_token="account-token",
                api_key_sid="SK123",
                api_key_secret="key-secret",
            ),
            ("SK123", "key-secret"),
        )

    def test_account_auth_token_is_the_fallback(self):
        self.assertEqual(
            sw.auth_credentials("AC123", auth_token="account-token"),
            ("AC123", "account-token"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
