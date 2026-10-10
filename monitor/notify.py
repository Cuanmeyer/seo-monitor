"""Alert output.

No external delivery. Alerts are written to the run log and to the GitHub
Actions run summary, which is a page per run that needs no account, no
webhook and no credentials. The dashboard remains the place you look; this
is the audit trail of what the monitor decided and when.

Delivery is a separate concern from detection, and it is deliberately not
wired up yet. Everything downstream of here takes the same `send(alerts)`
call, so adding a channel later is one file.
"""

from __future__ import annotations

import os

SEVERITY_LABEL = {"critical": "CRITICAL", "warning": "Warning", "info": "Info"}


def _headline(alerts: list[dict]) -> str:
    critical = [a for a in alerts if a["severity"] == "critical"]
    warnings = [a for a in alerts if a["severity"] == "warning"]

    if len(alerts) == 1:
        a = alerts[0]
        return f"{a.get('client_name', a.get('client', ''))}: {a.get('summary', '')}"

    bits = []
    if critical:
        bits.append(f"{len(critical)} critical")
    if warnings:
        bits.append(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
    lead = critical[0] if critical else alerts[0]
    return (
        f"{', '.join(bits)}, worst is "
        f"{lead.get('client_name', lead.get('client', ''))}"
    )


def _lines(alerts: list[dict]) -> list[str]:
    out: list[str] = []
    for a in alerts:
        out.append(
            f"{SEVERITY_LABEL.get(a['severity'], '')}: "
            f"{a.get('client_name', a.get('client', ''))}"
        )
        out.append(f"  {a.get('summary', '')}")
        if a.get("current_value") is not None and a.get("baseline_value") is not None:
            out.append(
                f"  Current {a['current_value']} vs baseline {a['baseline_value']}"
            )
        for detail in (a.get("detail") or [])[:5]:
            out.append(f"    - {detail}")
        if a.get("hint"):
            out.append(f"  {a['hint']}")
        if a.get("link"):
            out.append(f"  {a['link']}")
        out.append(f"  Seen {a.get('occurrences', 1)}x since {a.get('as_of', '')}")
        out.append("")
    return out


def _step_summary(alerts: list[dict]) -> None:
    """Write a readable block to the Actions run summary page.

    Best effort only. A filesystem problem here must never fail a run that
    has already collected and committed its data.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    rows = [
        "| Severity | Client | Signal | Detail | Seen |",
        "| --- | --- | --- | --- | --- |",
    ]
    for a in alerts:
        rows.append(
            "| {sev} | {client} | `{signal}` | {summary} | {times}x |".format(
                sev=SEVERITY_LABEL.get(a["severity"], ""),
                client=a.get("client_name", a.get("client", "")),
                signal=a.get("signal", ""),
                summary=str(a.get("summary", "")).replace("|", "\\|"),
                times=a.get("occurrences", 1),
            )
        )

    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"## {_headline(alerts)}\n\n")
            handle.write("\n".join(rows))
            handle.write("\n")
    except OSError as exc:
        print(f"[notify] could not write the run summary: {exc}")


def send(alerts: list[dict], dry_run: bool = False) -> int:
    """Record the run's alerts.

    Always returns 0. The count is what the caller reports as messages sent,
    and nothing is being sent anywhere yet, so claiming a delivery that did
    not happen would be worse than saying nothing.
    """
    if not alerts:
        return 0

    alerts = sorted(alerts, key=lambda a: 0 if a["severity"] == "critical" else 1)

    print(f"[notify] {_headline(alerts)}")
    for line in _lines(alerts):
        print(f"[notify]   {line}")

    if not dry_run:
        _step_summary(alerts)

    return 0
