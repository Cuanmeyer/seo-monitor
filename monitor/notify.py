"""Slack delivery.

Critical goes out immediately. Warnings are batched into one digest so a bad
morning produces one message rather than nine. Info is logged only.
"""

from __future__ import annotations

import os

import requests

SEVERITY_COLOR = {
    "critical": "#c0392b",
    "warning": "#d68910",
    "info": "#5d6d7e",
}

SEVERITY_LABEL = {
    "critical": "CRITICAL",
    "warning": "Warning",
    "info": "Info",
}


def _block(alert: dict) -> dict:
    lines = [f"*{alert['client_name']}* — {alert['summary']}"]

    if alert.get("current_value") is not None and alert.get("baseline_value") is not None:
        lines.append(
            f"Current `{alert['current_value']}` vs baseline `{alert['baseline_value']}`"
        )

    if alert.get("detail"):
        lines.append("")
        lines.append("*Top affected*")
        lines.extend(f"• {item}" for item in alert["detail"][:5])

    if alert.get("hint"):
        lines.append("")
        lines.append(f"_{alert['hint']}_")

    if alert.get("link"):
        lines.append(f"<{alert['link']}|Open in Search Console>")

    return {
        "color": SEVERITY_COLOR.get(alert["severity"], "#5d6d7e"),
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        ],
    }


def send(alerts: list[dict], dry_run: bool = False) -> int:
    """Post alerts to Slack. Returns how many messages were sent."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not alerts:
        return 0

    critical = [a for a in alerts if a["severity"] == "critical"]
    warnings = [a for a in alerts if a["severity"] == "warning"]

    messages = []
    for alert in critical:
        messages.append(
            {
                "text": f"{SEVERITY_LABEL['critical']}: {alert['client_name']} — {alert['summary']}",
                "attachments": [_block(alert)],
            }
        )

    if warnings:
        messages.append(
            {
                "text": f"SEO monitor: {len(warnings)} warning(s)",
                "attachments": [_block(alert) for alert in warnings],
            }
        )

    if dry_run or not webhook:
        for message in messages:
            print(f"[notify] would send: {message['text']}")
        return len(messages)

    sent = 0
    for message in messages:
        try:
            response = requests.post(webhook, json=message, timeout=20)
            if response.status_code < 300:
                sent += 1
            else:
                print(f"[notify] Slack rejected the message: {response.status_code} {response.text[:200]}")
        except requests.RequestException as exc:
            print(f"[notify] Slack delivery failed: {exc}")
    return sent
