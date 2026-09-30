"""Search Console: search analytics and URL inspection."""

from __future__ import annotations

import datetime as dt
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# Google's own cap is 2000/day/property. Stay well under it: the priority list
# is meant to be a sample, not a crawl.
MAX_INSPECTIONS_PER_RUN = 60


def client(sa_info: dict):
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES
    )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _query(api, site_url: str, body: dict) -> list[dict]:
    rows = []
    start_row = 0
    while True:
        payload = dict(body, startRow=start_row)
        response = (
            api.searchanalytics()
            .query(siteUrl=site_url, body=payload)
            .execute()
        )
        batch = response.get("rows", [])
        rows.extend(batch)
        if len(batch) < body.get("rowLimit", 1000):
            break
        start_row += len(batch)
        if start_row >= 25000:
            break
    return rows


def daily_totals(api, site_url: str, start: dt.date, end: dt.date) -> dict[str, dict]:
    """Per-day clicks, impressions, CTR and position.

    dataState FINAL means only confirmed data comes back, which is what a
    baseline needs. Fresh days would otherwise look like drops purely because
    Google has not finished counting them.
    """
    rows = _query(
        api,
        site_url,
        {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["date"],
            "type": "web",
            "dataState": "FINAL",
            "rowLimit": 1000,
        },
    )

    out = {}
    for row in rows:
        day = row["keys"][0]
        out[day] = {
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": row.get("ctr", 0.0),
            "position": row.get("position", 0.0),
        }
    return out


def page_window(api, site_url: str, start: dt.date, end: dt.date, limit: int = 250):
    """Clicks and impressions by page over a window, for alert detail."""
    rows = _query(
        api,
        site_url,
        {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["page"],
            "type": "web",
            "dataState": "FINAL",
            "rowLimit": limit,
        },
    )
    return {
        row["keys"][0]: {
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "position": row.get("position", 0.0),
        }
        for row in rows
    }


def query_window(api, site_url: str, start: dt.date, end: dt.date, limit: int = 100):
    rows = _query(
        api,
        site_url,
        {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["query"],
            "type": "web",
            "dataState": "FINAL",
            "rowLimit": limit,
        },
    )
    return {
        row["keys"][0]: {
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
        }
        for row in rows
    }


def inspect_urls(api, site_url: str, urls: list[str]) -> dict[str, dict]:
    """URL Inspection for the priority list.

    Failures are recorded per URL rather than raised, so one bad response does
    not take down the whole indexation signal.
    """
    results = {}
    for url in urls[:MAX_INSPECTIONS_PER_RUN]:
        try:
            response = (
                api.urlInspection()
                .index()
                .inspect(body={"inspectionUrl": url, "siteUrl": site_url})
                .execute()
            )
            index_status = (
                response.get("inspectionResult", {}).get("indexStatusResult", {})
            )
            results[url] = {
                "verdict": index_status.get("verdict"),
                "coverage_state": index_status.get("coverageState"),
                "robots_state": index_status.get("robotsTxtState"),
                "indexing_state": index_status.get("indexingState"),
                "google_canonical": index_status.get("googleCanonical"),
                "user_canonical": index_status.get("userCanonical"),
                "last_crawl": index_status.get("lastCrawlTime"),
                "ok": True,
            }
        except HttpError as exc:
            results[url] = {"ok": False, "error": f"{exc.status_code}: {exc.reason}"}
            if exc.status_code == 429:
                time.sleep(2)
        except Exception as exc:  # noqa: BLE001
            results[url] = {"ok": False, "error": str(exc)}
        time.sleep(0.25)  # stay polite against the per-minute quota
    return results


def indexed_rate(inspections: dict[str, dict]) -> float | None:
    checked = [r for r in inspections.values() if r.get("ok")]
    if not checked:
        return None
    passing = sum(1 for r in checked if r.get("verdict") == "PASS")
    return round(100.0 * passing / len(checked), 2)
