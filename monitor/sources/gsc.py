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

# Hard ceiling for any single pull, so a misconfigured caller cannot walk a
# large property forever.
ABSOLUTE_MAX_ROWS = 25000


def client(sa_info: dict):
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES
    )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _query(api, site_url: str, body: dict, max_rows: int | None = None) -> list[dict]:
    """Run a Search Analytics query, paging only as far as the caller needs.

    max_rows is the total number of rows wanted. Without it, an earlier version
    of this function treated rowLimit as a page size and kept paging until the
    property ran out of rows, so a request for 250 pages could fetch 12,000 and
    cost 50 API calls instead of one. Across a portfolio that was the
    difference between a 3 minute run and a 38 minute one.
    """
    cap = min(max_rows or ABSOLUTE_MAX_ROWS, ABSOLUTE_MAX_ROWS)
    rows: list[dict] = []
    start_row = 0

    while len(rows) < cap:
        page_size = min(body.get("rowLimit", 1000), cap - len(rows))
        payload = dict(body, rowLimit=page_size, startRow=start_row)
        response = (
            api.searchanalytics().query(siteUrl=site_url, body=payload).execute()
        )
        batch = response.get("rows", [])
        rows.extend(batch)

        if len(batch) < page_size:
            break  # property has no more rows
        start_row += len(batch)

    return rows[:cap]


def daily_totals(api, site_url: str, start: dt.date, end: dt.date) -> dict[str, dict]:
    """Per-day clicks, impressions, CTR and position.

    dataState FINAL means only confirmed data comes back, which is what a
    baseline needs. Fresh days would otherwise look like drops purely because
    Google has not finished counting them.
    """
    span = (end - start).days + 1
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
        max_rows=span + 5,
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
    """Clicks and impressions by page over a window, for alert detail.

    Sorted by clicks descending, so the first `limit` rows are the pages that
    matter. There is no reason to walk the long tail.
    """
    rows = _query(
        api,
        site_url,
        {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["page"],
            "type": "web",
            "dataState": "FINAL",
            "rowLimit": min(limit, 1000),
        },
        max_rows=limit,
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
            "rowLimit": min(limit, 1000),
        },
        max_rows=limit,
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
        time.sleep(0.1)
    return results


def indexed_rate(inspections: dict[str, dict]) -> float | None:
    checked = [r for r in inspections.values() if r.get("ok")]
    if not checked:
        return None
    passing = sum(1 for r in checked if r.get("verdict") == "PASS")
    return round(100.0 * passing / len(checked), 2)
