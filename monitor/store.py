"""JSON history store.

The baseline is the reason this exists. A rolling 28-day comparison cannot be
computed from live API calls alone, so every run appends that day's numbers
here and the evaluator reads back over them. Daily totals only, so the file
stays small enough to live in the repo and be diffed like any other change.
Page-level detail is kept as a single overwritten snapshot, not history.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .config import DATA_DIR

HISTORY_DIR = DATA_DIR / "history"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
ALERTS_PATH = DATA_DIR / "alerts.json"

# Trim history beyond this. 400 days keeps a full year of YoY comparison.
RETAIN_DAYS = 400


def _read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError:
            return fallback


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


def load_history(slug: str) -> dict:
    history = _read_json(HISTORY_DIR / f"{slug}.json", None)
    if history is None:
        history = {"client": slug, "days": {}, "state": {}}
    history.setdefault("days", {})
    history.setdefault("state", {})
    return history


def save_history(slug: str, history: dict) -> None:
    cutoff = (dt.date.today() - dt.timedelta(days=RETAIN_DAYS)).isoformat()
    history["days"] = {d: v for d, v in history["days"].items() if d >= cutoff}
    history["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _write_json(HISTORY_DIR / f"{slug}.json", history)


def record_day(history: dict, day: str, source: str, payload: dict) -> None:
    """Merge one source's numbers into one day. Re-running a day overwrites it,
    which matters because GSC keeps revising recent days upward."""
    bucket = history["days"].setdefault(day, {})
    bucket[source] = payload


def series(history: dict, source: str, metric: str) -> dict[str, float]:
    out = {}
    for day, payload in history["days"].items():
        value = (payload.get(source) or {}).get(metric)
        if value is not None:
            out[day] = value
    return out


def window_sum(data: dict[str, float], end: dt.date, days: int) -> tuple[float, int]:
    """Sum a metric over the N days ending on `end` inclusive.

    Returns the sum and how many days actually had data, so the caller can
    refuse to evaluate on a partial window rather than reporting a false drop.
    """
    total = 0.0
    found = 0
    for offset in range(days):
        key = (end - dt.timedelta(days=offset)).isoformat()
        if key in data:
            total += data[key]
            found += 1
    return total, found


def load_snapshot(slug: str) -> dict:
    return _read_json(SNAPSHOT_DIR / f"{slug}.json", {})


def save_snapshot(slug: str, payload: dict) -> None:
    _write_json(SNAPSHOT_DIR / f"{slug}.json", payload)


def load_alerts() -> list[dict]:
    return _read_json(ALERTS_PATH, [])


def save_alerts(alerts: list[dict]) -> None:
    _write_json(ALERTS_PATH, alerts[-2000:])


def append_alerts(new_alerts: list[dict]) -> list[dict]:
    """Add alerts to the log, collapsing repeats of an already-open alert.

    An ongoing problem stays one alert with a bumped last_seen, so a week-long
    drop does not produce seven identical Slack messages.
    """
    existing = load_alerts()
    open_index = {
        (a["client"], a["signal"]): a
        for a in existing
        if a.get("status") == "open"
    }

    fresh = []
    for alert in new_alerts:
        key = (alert["client"], alert["signal"])
        prior = open_index.get(key)
        if prior:
            prior["last_seen"] = alert["detected_at"]
            prior["occurrences"] = prior.get("occurrences", 1) + 1
            prior["current_value"] = alert["current_value"]
            prior["pct_change"] = alert["pct_change"]
            # Keep the wording current. Without this an alert raised for two
            # URLs still said "2" weeks after one of them was fixed.
            for field in ("summary", "detail", "hint", "as_of", "baseline_value"):
                if field in alert:
                    prior[field] = alert[field]
            if alert["severity"] == "critical" and prior["severity"] != "critical":
                prior["severity"] = "critical"
                fresh.append(prior)  # escalation is worth re-notifying
        else:
            alert["status"] = "open"
            alert["occurrences"] = 1
            alert["last_seen"] = alert["detected_at"]
            existing.append(alert)
            open_index[key] = alert
            fresh.append(alert)

    save_alerts(existing)
    return fresh


def resolve_alerts(
    client: str,
    healthy_signals: set[str],
    today: str,
    raised_signals: set[str] | None = None,
    watching_signals: set[str] | None = None,
    stale_after_days: int = 3,
) -> list[dict]:
    """Close open alerts whose signal has come back inside threshold.

    Also closes alerts that have gone quiet: not raised again for
    stale_after_days and not still breaching. Without this, a signal that
    stops being judged at all (for example LLM referrals dropping under the
    volume floor) left its last alert open forever.
    """
    raised_signals = raised_signals or set()
    watching_signals = watching_signals or set()
    today_date = dt.date.fromisoformat(today[:10])

    alerts = load_alerts()
    resolved = []
    for alert in alerts:
        if alert.get("status") != "open" or alert["client"] != client:
            continue
        signal = alert["signal"]
        reason = None
        if signal in healthy_signals:
            reason = "back inside threshold"
        elif signal not in raised_signals and signal not in watching_signals:
            last_seen = (alert.get("last_seen") or alert.get("detected_at") or "")[:10]
            try:
                quiet = (today_date - dt.date.fromisoformat(last_seen)).days
            except ValueError:
                quiet = 0
            if quiet >= stale_after_days:
                reason = f"not seen for {quiet} days"
        if reason:
            alert["status"] = "resolved"
            alert["resolved_at"] = today
            alert["resolved_reason"] = reason
            resolved.append(alert)
    if resolved:
        save_alerts(alerts)
    return resolved
