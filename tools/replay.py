#!/usr/bin/env python3
"""Replay stored history one day at a time.

Simulates consecutive daily runs so the persistence counters behave exactly as
they would in production. This is how thresholds get tuned: you can see which
days would have alerted and, just as importantly, which quiet days would have
stayed quiet.

    python tools/replay.py arcstore
    python tools/replay.py arcstore --drop 30 --from 2026-09-05
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor import evaluate, store  # noqa: E402
from monitor.config import load_clients  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("client")
    parser.add_argument("--days", type=int, default=30, help="how many days to replay")
    parser.add_argument(
        "--drop",
        type=float,
        default=0,
        help="inject a synthetic %% drop in clicks from --from onward",
    )
    parser.add_argument("--from", dest="drop_from", help="date the injected drop starts")
    args = parser.parse_args()

    client_cfg = load_clients(args.client)[0]
    history = store.load_history(client_cfg.slug)
    snapshot = store.load_snapshot(client_cfg.slug)

    if args.drop:
        start = dt.date.fromisoformat(args.drop_from)
        for day, payload in history["days"].items():
            if dt.date.fromisoformat(day) >= start and "gsc" in payload:
                payload["gsc"] = dict(payload["gsc"])
                payload["gsc"]["clicks"] = round(
                    payload["gsc"]["clicks"] * (1 - args.drop / 100)
                )
        print(f"Injected a {args.drop:.0f}% clicks drop from {start} onward\n")

    days = sorted(history["days"].keys())
    replay_days = days[-args.days:]

    # Fresh state so streaks start from zero, and a throwaway copy so the
    # replay never writes to the real history file.
    working = copy.deepcopy(history)
    working["state"] = {}

    print(f"{'Date':<12} {'Clicks':>8} {'vs base':>9} {'Impr':>9} {'vs base':>9}  Fired")
    print("-" * 74)

    fired_total = 0
    for day in replay_days:
        as_of = dt.date.fromisoformat(day)
        findings, _healthy, watching = evaluate.evaluate_client(
            client_cfg, working, snapshot, as_of, {"gsc": {"ok": True}}
        )

        clicks = evaluate._windows(client_cfg, working, "gsc", "clicks", as_of)
        impressions = evaluate._windows(client_cfg, working, "gsc", "impressions", as_of)

        c_now = f"{clicks[0]:,.0f}" if clicks else "—"
        c_pct = f"{evaluate.pct_change(*clicks):+.1f}%" if clicks else "—"
        i_now = f"{impressions[0]:,.0f}" if impressions else "—"
        i_pct = f"{evaluate.pct_change(*impressions):+.1f}%" if impressions else "—"

        labels = ", ".join(f"{f.signal}[{f.severity[:4]}]" for f in findings)
        if watching:
            labels += ("  " if labels else "") + "watching: " + ",".join(sorted(watching))
        fired_total += len(findings)
        print(f"{day:<12} {c_now:>8} {c_pct:>9} {i_now:>9} {i_pct:>9}  {labels}")

    print("-" * 74)
    print(f"{fired_total} alert(s) across {len(replay_days)} simulated runs")

    if fired_total:
        print("\nMost recent finding detail:")
        for finding in findings:
            print(f"  [{finding.severity.upper()}] {finding.summary}")
            for line in finding.detail[:5]:
                print(f"      - {line}")
            if finding.hint:
                print(f"      hint: {finding.hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
