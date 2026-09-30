"""Core Web Vitals from the Chrome UX Report API.

Field data, not lab data, so this is what Google actually uses. Needs only an
API key. CrUX reports a 28-day trailing window, so it moves slowly and is
checked weekly rather than daily.
"""

from __future__ import annotations

import os

import requests

ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"

METRICS = {
    "largest_contentful_paint": "LCP",
    "interaction_to_next_paint": "INP",
    "cumulative_layout_shift": "CLS",
}

# Google's own Good/Needs Improvement boundaries.
GOOD_THRESHOLD = {"LCP": 2500, "INP": 200, "CLS": 0.1}
POOR_THRESHOLD = {"LCP": 4000, "INP": 500, "CLS": 0.25}


def _rating(metric: str, p75: float) -> str:
    if p75 <= GOOD_THRESHOLD[metric]:
        return "good"
    if p75 <= POOR_THRESHOLD[metric]:
        return "needs_improvement"
    return "poor"


def fetch(target: str, form_factor: str = "PHONE", origin: bool = False) -> dict | None:
    """Query CrUX for a URL or an origin. Returns None when there is no data,
    which is normal for low-traffic URLs and is not an error."""
    api_key = os.environ.get("CRUX_API_KEY")
    if not api_key:
        return None

    body = {"formFactor": form_factor}
    body["origin" if origin else "url"] = target

    try:
        response = requests.post(
            ENDPOINT, params={"key": api_key}, json=body, timeout=30
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}

    if response.status_code == 404:
        return None  # not enough traffic for a CrUX record
    if response.status_code != 200:
        return {"ok": False, "error": f"{response.status_code}: {response.text[:200]}"}

    record = response.json().get("record", {})
    metrics = record.get("metrics", {})

    out = {"ok": True, "form_factor": form_factor, "metrics": {}}
    for api_name, short in METRICS.items():
        block = metrics.get(api_name)
        if not block:
            continue
        p75 = block.get("percentiles", {}).get("p75")
        if p75 is None:
            continue
        p75 = float(p75)
        histogram = block.get("histogram", [])
        good = float(histogram[0].get("density", 0)) * 100 if histogram else None
        poor = float(histogram[2].get("density", 0)) * 100 if len(histogram) > 2 else None
        out["metrics"][short] = {
            "p75": round(p75, 3),
            "rating": _rating(short, p75),
            "good_pct": round(good, 1) if good is not None else None,
            "poor_pct": round(poor, 1) if poor is not None else None,
        }

    return out if out["metrics"] else None
