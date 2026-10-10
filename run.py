#!/usr/bin/env python3
"""Entry point.

    python run.py                          full daily run, every client
    python run.py --client arcstore        one client
    python run.py --backfill 120           deeper history pull on first run
    python run.py --dry-run                evaluate and print, send nothing
    python run.py --no-collect             evaluate stored history only
    python run.py --as-of 2026-09-18       replay a past date against history
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from monitor import evaluate, notify, render, store
from monitor.collect import collect_client
from monitor.config import load_clients


def parse_args():
    parser = argparse.ArgumentParser(description="Client SEO health monitor")
    parser.add_argument("--client", help="run a single client by slug")
    parser.add_argument("--backfill", type=int, default=0, help="days of history to pull")
    parser.add_argument("--dry-run", action="store_true", help="do not send alerts")
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help="skip API calls and evaluate the stored history",
    )
    parser.add_argument("--as-of", help="evaluate as if it were this date (YYYY-MM-DD)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    clients = load_clients(args.client)

    all_new_alerts = []
    summary = {}

    for client_cfg in clients:
        lag = client_cfg.setting("gsc_lag_days", 3)
        if args.as_of:
            as_of = dt.date.fromisoformat(args.as_of)
        else:
            as_of = dt.date.today() - dt.timedelta(days=lag)

        print(f"\n=== {client_cfg.name} ({client_cfg.slug}) as of {as_of} ===")

        history = store.load_history(client_cfg.slug)

        if args.no_collect:
            snapshot = store.load_snapshot(client_cfg.slug)
            source_status = snapshot.get("source_status") or {"gsc": {"ok": True}}
        else:
            snapshot, source_status = collect_client(
                client_cfg, history, as_of, args.backfill
            )
            store.save_snapshot(client_cfg.slug, snapshot)

        for source, status in source_status.items():
            state = "ok" if status.get("ok") else f"FAILED ({status.get('error')})"
            print(f"  {source}: {state}")

        # Google finalises Search Console data two to three days back, but not
        # on a timetable. Some mornings the T-3 day has simply not landed yet,
        # and evaluating it leaves the 7-day window one day short, at which
        # point every GSC signal correctly refuses to evaluate and the whole
        # dashboard reads "no data". So evaluate the newest day the data
        # actually covers instead of the newest day the calendar suggests.
        #
        # The step back is bounded. A collector that has been dead for a week
        # must keep reading as no data rather than quietly evaluating a stale
        # window, and the collector failure alert is what speaks to that.
        if not args.as_of:
            covered = [
                day
                for day in store.series(history, "gsc", "clicks")
                if day <= as_of.isoformat()
            ]
            if covered:
                latest = dt.date.fromisoformat(max(covered))
                behind = (as_of - latest).days
                if 0 < behind <= client_cfg.setting("max_extra_lag_days", 4):
                    print(f"  GSC is final only to {latest}, evaluating that day")
                    as_of = latest

        findings, healthy, watching = evaluate.evaluate_client(
            client_cfg, history, snapshot, as_of, source_status
        )
        for signal in sorted(watching):
            print(f"  watching: {signal} is breaching but has not met its persistence bar")

        suppressed_reason = client_cfg.is_suppressed(as_of)
        alerts = [
            finding.to_alert(client_cfg.slug, client_cfg.name, as_of.isoformat())
            for finding in findings
        ]

        if suppressed_reason:
            for alert in alerts:
                alert["suppressed"] = suppressed_reason
            print(f"  suppression window active: {suppressed_reason}")

        resolved = store.resolve_alerts(client_cfg.slug, healthy, as_of.isoformat())
        for alert in resolved:
            print(f"  resolved: {alert['signal']}")

        new_alerts = store.append_alerts(alerts) if alerts else []
        store.save_history(client_cfg.slug, history)

        if not findings:
            print("  all monitored signals inside threshold")
        for finding in findings:
            print(f"  [{finding.severity.upper()}] {finding.signal}: {finding.summary}")
            for line in finding.detail[:5]:
                print(f"      - {line}")
            if finding.hint:
                print(f"      hint: {finding.hint}")

        summary[client_cfg.slug] = {
            "name": client_cfg.name,
            "as_of": as_of.isoformat(),
            "healthy": sorted(healthy),
            "watching": sorted(watching),
            "findings": [f.signal for f in findings],
            "source_status": source_status,
        }

        if not suppressed_reason:
            all_new_alerts.extend(new_alerts)

    sent = notify.send(all_new_alerts, dry_run=args.dry_run)
    if sent:
        print(f"\nSent {sent} Slack message(s)")

    render.build(clients, summary)
    print("Dashboard written to public/index.html")

    # A monitor failure should make the scheduled run go red in the Actions
    # log, so a broken collector is visible even if Slack is misconfigured.
    failed = [
        alert for alert in all_new_alerts if alert["signal"].startswith("monitor_failure")
    ]
    if failed:
        print(json.dumps(failed, indent=2), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
