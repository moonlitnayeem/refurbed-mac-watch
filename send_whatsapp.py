#!/usr/bin/env python3
"""Send new Mac Studio alerts through Meta's WhatsApp Cloud API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def fmt_kr(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " kr"


def template_payload(alert: dict, to: str, template_name: str,
                     language: str = "en_US") -> dict:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": alert["model"]},
                    {"type": "text", "text": fmt_kr(alert["price"])},
                    {"type": "text", "text": alert["url"]},
                ],
            }],
        },
    }


def send_template(phone_number_id: str, token: str, payload: dict,
                  api_version: str = "v25.0") -> str:
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"WhatsApp API returned HTTP {error.code}: {detail}") from error
    messages = result.get("messages") or []
    if not messages or not messages[0].get("id"):
        raise RuntimeError(f"WhatsApp API response had no message id: {result}")
    return messages[0]["id"]


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def main() -> int:
    alert_path = Path(os.environ.get("WHATSAPP_ALERT_FILE", "mac_studio_alerts.json"))
    if not alert_path.exists():
        print("No new Mac Studio alert to send.")
        return 0

    token = required_env("META_WHATSAPP_TOKEN")
    phone_number_id = required_env("META_WHATSAPP_PHONE_NUMBER_ID")
    to = required_env("META_WHATSAPP_TO")
    template_name = os.environ.get("META_WHATSAPP_TEMPLATE_NAME", "mac_studio_alert")
    language = os.environ.get("META_WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
    api_version = os.environ.get("META_GRAPH_API_VERSION", "v25.0")

    alerts = json.loads(alert_path.read_text(encoding="utf-8"))
    for alert in alerts:
        payload = template_payload(alert, to, template_name, language)
        message_id = send_template(phone_number_id, token, payload, api_version)
        print(f"Sent WhatsApp Mac Studio alert: {message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
