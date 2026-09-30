"""GA4: organic sessions and LLM referral traffic.

The LLM referral pull is the working half of the brief's Gen AI signal. The
Search Console Generative AI report is not exposed as its own search type in
the Search Analytics API, so AI Overview impressions cannot be isolated from
web impressions programmatically. That half stays manual until Google ships
an API for it, and the dashboard says so rather than showing a silent zero.
"""

from __future__ import annotations

import datetime as dt

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    RunReportRequest,
)
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

LLM_REFERRERS = [
    "chatgpt.com",
    "perplexity.ai",
    "gemini.google.com",
    "copilot.microsoft.com",
    "claude.ai",
]


def client(sa_info: dict) -> BetaAnalyticsDataClient:
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES
    )
    return BetaAnalyticsDataClient(credentials=creds)


def organic_sessions(api, property_id: str, start: dt.date, end: dt.date):
    """Daily organic search sessions and conversions."""
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
        dimensions=[Dimension(name="date")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="conversions"),
            Metric(name="totalRevenue"),
        ],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="sessionDefaultChannelGroup",
                string_filter=Filter.StringFilter(value="Organic Search"),
            )
        ),
        limit=500,
    )
    response = api.run_report(request)

    out = {}
    for row in response.rows:
        raw_date = row.dimension_values[0].value
        day = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        out[day] = {
            "sessions": int(row.metric_values[0].value or 0),
            "conversions": float(row.metric_values[1].value or 0),
            "revenue": float(row.metric_values[2].value or 0),
        }
    return out


def llm_referral_sessions(api, property_id: str, start: dt.date, end: dt.date):
    """Daily sessions arriving from LLM surfaces."""
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
        dimensions=[Dimension(name="date"), Dimension(name="sessionSource")],
        metrics=[Metric(name="sessions")],
        dimension_filter=FilterExpression(
            or_group=FilterExpressionList(
                expressions=[
                    FilterExpression(
                        filter=Filter(
                            field_name="sessionSource",
                            string_filter=Filter.StringFilter(
                                value=host, match_type=Filter.StringFilter.MatchType.CONTAINS
                            ),
                        )
                    )
                    for host in LLM_REFERRERS
                ]
            )
        ),
        limit=5000,
    )
    response = api.run_report(request)

    totals: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in response.rows:
        raw_date = row.dimension_values[0].value
        day = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        source = row.dimension_values[1].value
        sessions = int(row.metric_values[0].value or 0)
        totals[day] = totals.get(day, 0) + sessions
        by_source[source] = by_source.get(source, 0) + sessions

    return (
        {day: {"llm_sessions": count} for day, count in totals.items()},
        by_source,
    )
