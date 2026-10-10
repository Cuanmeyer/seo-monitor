"""Dashboard build.

Reads the history store and the alert log and writes a single static
public/index.html. No build step, no runtime data fetching: whatever Netlify
serves is exactly what the last run computed.

The daily series for each client is embedded in the page in full, which is
what lets the dashboard offer a 7 / 14 / 28 day view toggle without any
further API calls. The alerting window is deliberately not affected by that
toggle: thresholds and persistence are tuned against 7 days versus the prior
28, and a viewing control that quietly changed what fires would make the
alert set depend on whoever last clicked something.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import store
from .config import PUBLIC_DIR

TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"

SIGNALS = [
    ("organic_clicks", "Clicks"),
    ("organic_impressions", "Impressions"),
    ("ctr_divergence", "CTR"),
    ("indexation_priority", "Indexation"),
    ("indexation_rate", "Index rate"),
    ("core_web_vitals", "Core Web Vitals"),
    ("llm_referrals", "LLM referrals"),
]

CHART_DAYS = 90


def _rolling_baseline(dates: list[str], values: list[float], current: int, baseline: int):
    """The same comparison the evaluator uses, computed for every day so the
    chart shows the band the thresholds were actually measured against."""
    index = {d: v for d, v in zip(dates, values)}
    out = []
    for day in dates:
        end = dt.date.fromisoformat(day) - dt.timedelta(days=current)
        total = 0.0
        found = 0
        for offset in range(baseline):
            key = (end - dt.timedelta(days=offset)).isoformat()
            if key in index:
                total += index[key]
                found += 1
        out.append(round(total / found, 1) if found == baseline else None)
    return out


def _status_for(signal: str, client_slug: str, summary: dict, open_alerts: list[dict]) -> dict:
    for alert in open_alerts:
        if alert["client"] == client_slug and alert["signal"] == signal:
            return {"state": alert["severity"], "note": alert["summary"]}

    info = summary.get(client_slug, {})
    # Watching is checked first. A non-alerting signal is reported as both
    # watching and healthy, the second so an alert raised under an older
    # configuration can be retired, and watching is the truer of the two.
    if signal in info.get("watching", []):
        return {
            "state": "watching",
            "note": "Outside threshold, but not raising an alert",
        }
    if signal in info.get("healthy", []):
        return {"state": "good", "note": "Inside threshold"}
    return {"state": "unknown", "note": "Not enough history yet"}


def build(clients, summary: dict) -> Path:
    alerts = store.load_alerts()
    open_alerts = [a for a in alerts if a.get("status") == "open"]

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "signals": [{"key": k, "label": label} for k, label in SIGNALS],
        "clients": [],
    }

    for client_cfg in clients:
        history = store.load_history(client_cfg.slug)
        snapshot = store.load_snapshot(client_cfg.slug)
        info = summary.get(client_cfg.slug, {})

        # Only days Search Console has actually reported. GA4 finalises a day
        # earlier than GSC, so the newest entry in the history often carries
        # GA4 figures and no GSC ones. Charting it as a zero drew a cliff to
        # the floor on the right hand edge of every graph, and pulled the
        # 7-day headline down with it.
        days = [
            day
            for day in sorted(history["days"].keys())
            if (history["days"][day].get("gsc") or {}).get("clicks") is not None
        ][-CHART_DAYS:]
        clicks = [history["days"][d].get("gsc", {}).get("clicks", 0) for d in days]
        impressions = [
            history["days"][d].get("gsc", {}).get("impressions", 0) for d in days
        ]
        position = [history["days"][d].get("gsc", {}).get("position") for d in days]

        current_days = client_cfg.setting("current_window_days", 7)
        baseline_days = client_cfg.setting("baseline_window_days", 28)

        client_alerts = [a for a in open_alerts if a["client"] == client_cfg.slug]

        # Headline numbers, same windows the evaluator used.
        def window(values: list[float], offset: int, length: int):
            slice_ = values[len(values) - offset - length : len(values) - offset]
            return sum(slice_) if len(slice_) == length else None

        clicks_now = window(clicks, 0, current_days)
        clicks_base = window(clicks, current_days, baseline_days)
        impressions_now = window(impressions, 0, current_days)
        impressions_base = window(impressions, current_days, baseline_days)
        scale = baseline_days / current_days

        recent_positions = [p for p in position[-current_days:] if p]

        payload["clients"].append(
            {
                "slug": client_cfg.slug,
                "name": client_cfg.name,
                "domain": client_cfg.domain,
                "as_of": info.get("as_of"),
                "statuses": {
                    key: _status_for(key, client_cfg.slug, summary, open_alerts)
                    for key, _ in SIGNALS
                },
                "source_status": info.get("source_status", {}),
                "open_alerts": client_alerts,
                "kpis": {
                    "clicks": {
                        "current": clicks_now,
                        "baseline": round(clicks_base / scale) if clicks_base else None,
                    },
                    "impressions": {
                        "current": impressions_now,
                        "baseline": round(impressions_base / scale)
                        if impressions_base
                        else None,
                    },
                    "position": round(sum(recent_positions) / len(recent_positions), 1)
                    if recent_positions
                    else None,
                    "indexed_rate": snapshot.get("indexed_rate"),
                },
                "series": {
                    "dates": days,
                    "clicks": clicks,
                    "impressions": impressions,
                    # Carried so the dashboard's window toggle can recompute
                    # average position for whatever span the viewer picks.
                    "position": position,
                    "clicks_baseline": _rolling_baseline(
                        days, clicks, current_days, baseline_days
                    ),
                    "impressions_baseline": _rolling_baseline(
                        days, impressions, current_days, baseline_days
                    ),
                },
                "windows": {
                    "current_days": current_days,
                    "baseline_days": baseline_days,
                },
                "thresholds": {
                    "warning_pct": client_cfg.threshold("organic_clicks", "warning_pct", -20),
                    "critical_pct": client_cfg.threshold("organic_clicks", "critical_pct", -35),
                },
            }
        )

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace(
        "/*__DATA__*/null",
        json.dumps(payload, ensure_ascii=False),
    )

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    out = PUBLIC_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    return out
