#!/usr/bin/env python3
"""Send new Mac Studio alerts through Twilio's WhatsApp API."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def fmt_kr(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " kr"


def whatsapp_address(number: str) -> str:
    number = number.removeprefix("whatsapp:")
    if not number.startswith("+"):
        number = "+" + number
    return "whatsapp:" + number


def message_form(alert: dict, to: str, sender: str,
                 content_sid: str | None = None,
                 template_style: str = "mac_studio") -> dict[str, str]:
    form = {
        "To": whatsapp_address(to),
        "From": whatsapp_address(sender),
    }
    if content_sid:
        if template_style == "appointment":
            variables = {
                "1": (
                    f"Mac Studio available: {alert['model']} — "
                    f"{fmt_kr(alert['price'])}"
                ),
                "2": alert["url"],
            }
        else:
            variables = {
                "1": alert["model"],
                "2": fmt_kr(alert["price"]),
                "3": alert["url"],
            }
        form.update({
            "ContentSid": content_sid,
            "ContentVariables": json.dumps(variables, ensure_ascii=False),
        })
    else:
        form["Body"] = (
            f"Mac Studio available: {alert['model']}\n"
            f"Price: {fmt_kr(alert['price'])}\n"
            f"{alert['url']}"
        )
    return form


def basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return "Basic " + token


def auth_credentials(account_sid: str, auth_token: str | None = None,
                     api_key_sid: str | None = None,
                     api_key_secret: str | None = None) -> tuple[str, str]:
    if api_key_sid and api_key_secret:
        return api_key_sid, api_key_secret
    if auth_token:
        return account_sid, auth_token
    raise RuntimeError(
        "Set TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET, or TWILIO_AUTH_TOKEN"
    )


def twilio_request(url: str, auth_username: str, auth_secret: str,
                   *, method: str = "GET", form: dict[str, str] | None = None) -> dict:
    data = None
    headers = {
        "Authorization": basic_auth(auth_username, auth_secret),
        "Accept": "application/json",
    }
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Twilio API returned HTTP {error.code}: {detail}") from error


def send_message(account_sid: str, auth_username: str, auth_secret: str,
                 form: dict[str, str]) -> dict:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    result = twilio_request(
        url, auth_username, auth_secret, method="POST", form=form
    )
    if not result.get("sid"):
        raise RuntimeError(f"Twilio API response had no message SID: {result}")
    return result


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

    account_sid = required_env("TWILIO_ACCOUNT_SID")
    auth_username, auth_secret = auth_credentials(
        account_sid,
        auth_token=os.environ.get("TWILIO_AUTH_TOKEN"),
        api_key_sid=os.environ.get("TWILIO_API_KEY_SID"),
        api_key_secret=os.environ.get("TWILIO_API_KEY_SECRET"),
    )
    sender = required_env("TWILIO_WHATSAPP_FROM")
    to = required_env("TWILIO_WHATSAPP_TO")
    content_sid = os.environ.get("TWILIO_CONTENT_SID") or None
    template_style = os.environ.get("TWILIO_TEMPLATE_STYLE", "mac_studio")

    alerts = json.loads(alert_path.read_text(encoding="utf-8"))
    for alert in alerts:
        form = message_form(alert, to, sender, content_sid, template_style)
        result = send_message(account_sid, auth_username, auth_secret, form)
        print(
            "Sent Twilio WhatsApp Mac Studio alert: "
            f"{result['sid']} ({result.get('status', 'status unknown')})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
