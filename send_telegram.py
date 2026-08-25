#!/usr/bin/env python3
"""Send new Mac Studio alerts through the Telegram Bot API."""

from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def fmt_kr(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " kr"


def message_payload(alert: dict, chat_id: str) -> dict:
    model = html.escape(str(alert["model"]))
    url = html.escape(str(alert["url"]), quote=True)
    custom_title = alert.get("title")
    title = html.escape(str(custom_title or "Mac Studio available"))
    icon = "🧪" if custom_title else "🔔"
    text = (
        f"{icon} <b>{title}</b>\n"
        f"{model}\n"
        f"<b>Price:</b> {fmt_kr(alert['price'])}\n"
        f'<a href="{url}">Open on Refurbed</a>'
    )
    return {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


def telegram_request(token: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Telegram Bot API returned HTTP {error.code}: {detail}"
        ) from error
    if not body.get("ok") or not body.get("result", {}).get("message_id"):
        raise RuntimeError(f"Telegram Bot API rejected the message: {body}")
    return body["result"]


def main() -> int:
    alert_path = Path(os.environ.get("PHONE_ALERT_FILE", "mac_studio_alerts.json"))
    if not alert_path.exists():
        print("No new Mac Studio alert to send.")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when alerts exist"
        )

    alerts = json.loads(alert_path.read_text(encoding="utf-8"))
    for alert in alerts:
        result = telegram_request(token, message_payload(alert, chat_id))
        print(
            "Sent Telegram Mac Studio alert: "
            f"message {result['message_id']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
