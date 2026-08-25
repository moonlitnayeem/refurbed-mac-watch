#!/usr/bin/env python3

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import send_telegram as st


class TelegramPayloadTest(unittest.TestCase):
    def test_mac_studio_payload_contains_actionable_details(self):
        alert = {
            "model": "M3 Ultra <256 GB>",
            "price": 54_900,
            "url": "https://www.refurbed.se/p/apple-mac-studio/?a=1&b=2",
        }

        payload = st.message_payload(alert, chat_id="123456")

        self.assertEqual(payload["chat_id"], "123456")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(payload["disable_web_page_preview"])
        self.assertEqual(
            payload["text"],
            "🔔 <b>Mac Studio available</b>\n"
            "M3 Ultra &lt;256 GB&gt;\n"
            "<b>Price:</b> 54 900 kr\n"
            '<a href="https://www.refurbed.se/p/apple-mac-studio/?a=1&amp;b=2">'
            "Open on Refurbed</a>",
        )

    def test_payload_can_be_labelled_as_a_delivery_test(self):
        alert = {
            "title": "Telegram delivery test",
            "test": True,
            "model": "Integration check",
            "price": 0,
            "url": "https://example.com/status",
        }

        payload = st.message_payload(alert, chat_id="123456")

        self.assertTrue(payload["text"].startswith(
            "🧪 <b>Telegram delivery test</b>\n"
        ))

    def test_threshold_alert_keeps_bell_icon_with_custom_title(self):
        alert = {
            "title": "M2 Max 64 GB below 23 000 kr",
            "model": "MacBook Pro · M2 Max · 64 GB · 1 TB SSD",
            "price": 22_899,
            "url": "https://www.refurbed.se/p/apple-macbook-pro/x/",
        }

        payload = st.message_payload(alert, chat_id="123456")

        self.assertTrue(payload["text"].startswith(
            "🔔 <b>M2 Max 64 GB below 23 000 kr</b>\n"
        ))
        self.assertIn("<b>Price:</b> 22 899 kr", payload["text"])

    @mock.patch("send_telegram.urllib.request.urlopen")
    def test_send_message_posts_json_to_bot_api(self, urlopen_mock):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok":true,"result":{"message_id":42}}'

        urlopen_mock.return_value = Response()
        payload = {"chat_id": "123", "text": "hello"}

        result = st.telegram_request("123:fake-token", payload)

        self.assertEqual(result["message_id"], 42)
        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.telegram.org/bot123:fake-token/sendMessage",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 30)

    @mock.patch("send_telegram.urllib.request.urlopen")
    def test_send_message_raises_when_telegram_rejects_it(self, urlopen_mock):
        response = urlopen_mock.return_value.__enter__.return_value
        response.read.return_value = b'{"ok":false,"description":"chat not found"}'

        with self.assertRaisesRegex(RuntimeError, "chat not found"):
            st.telegram_request("123:fake-token", {"chat_id": "bad"})

    def test_no_alert_file_exits_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("builtins.print") as print_mock:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                result = st.main()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(result, 0)
        print_mock.assert_called_once_with("No Telegram alert to send.")

    @mock.patch("send_telegram.telegram_request")
    def test_main_sends_every_pending_alert(self, request_mock):
        alerts = [
            {"model": "M1 Max · 64 GB", "price": 19_845,
             "url": "https://example.com/one"},
            {"model": "M3 Ultra · 256 GB", "price": 54_900,
             "url": "https://example.com/two"},
        ]
        request_mock.side_effect = [
            {"message_id": 41},
            {"message_id": 42},
        ]

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(
                 os.environ,
                 {"TELEGRAM_BOT_TOKEN": "123:fake", "TELEGRAM_CHAT_ID": "456"},
                 clear=True,
             ), mock.patch("builtins.print"):
            path = Path(tmp) / "alerts.json"
            path.write_text(json.dumps(alerts), encoding="utf-8")
            os.environ["PHONE_ALERT_FILE"] = str(path)
            result = st.main()

        self.assertEqual(result, 0)
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(request_mock.call_args_list[0].args[0], "123:fake")
        self.assertEqual(
            request_mock.call_args_list[1].args[1]["chat_id"], "456"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
