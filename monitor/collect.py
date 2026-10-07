"""Collection orchestration.

Every source is wrapped. A source that fails records its error and the run
continues, because one dead API should not stop the other five signals from
being checked. The failure itself becomes a critical alert in the evaluator so
a gap can never be mistaken for a clean bill of health.
"""

from __future__ import annotations

import datetime as dt

from . import store
from .config import service_account_info


def collect_client(client_cfg, history: dict, as_of: dt.date, backfill_days: int = 0):
    """Pull every configured source into history and build the run snapshot."""
    # Imported here rather than at module scope so that --no-collect runs, and
    # the evaluator's tests, work without the Google SDKs installed.
    from .sources import crux, ga4, gsc

    source_status: dict[str, dict] = {}
    snapshot: dict = {"domain": client_cfg.domain, "as_of": as_of.isoformat()}

    sa_info = service_account_info()
    if not sa_info:
        raise SystemExit(
            "No service account credentials. Set GOOGLE_SERVICE_ACCOUNT_JSON "
            "to the full JSON key, or GOOGLE_APPLICATION_CREDENTIALS to a path."
        )

    current_days = client_cfg.setting("current_window_days", 7)
    baseline_days = client_cfg.setting("baseline_window_days", 28)

    # Pull enough history to fill both windows on a cold start, more on backfill.
    span = max(backfill_days, current_days + baseline_days + 7)
    start = as_of - dt.timedelta(days=span)

    # --- Search Console ---------------------------------------------------
    try:
        api = gsc.client(sa_info)

        daily = gsc.daily_totals(api, client_cfg.gsc_property, start, as_of)
        for day, values in daily.items():
            store.record_day(history, day, "gsc", values)

        current_start = as_of - dt.timedelta(days=current_days - 1)
        baseline_end = as_of - dt.timedelta(days=current_days)
        baseline_start = baseline_end - dt.timedelta(days=baseline_days - 1)

        snapshot["pages_current"] = gsc.page_window(
            api, client_cfg.gsc_property, current_start, as_of
        )
        snapshot["pages_baseline"] = gsc.page_window(
            api, client_cfg.gsc_property, baseline_start, baseline_end
        )
        snapshot["baseline_scale"] = baseline_days / current_days

        if client_cfg.priority_urls:
            inspections = gsc.inspect_urls(
                api, client_cfg.gsc_property, client_cfg.priority_urls
            )
            snapshot["inspections"] = inspections
            snapshot["indexed_rate"] = gsc.indexed_rate(inspections)

        source_status["gsc"] = {"ok": True, "days_pulled": len(daily)}
    except Exception as exc:  # noqa: BLE001
        source_status["gsc"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- GA4 --------------------------------------------------------------
    if client_cfg.ga4_property_id:
        try:
            api = ga4.client(sa_info)

            sessions = ga4.organic_sessions(api, client_cfg.ga4_property_id, start, as_of)
            llm_daily, llm_by_source = ga4.llm_referral_sessions(
                api, client_cfg.ga4_property_id, start, as_of
            )

            for day in set(sessions) | set(llm_daily):
                merged = dict(sessions.get(day, {}))
                merged.update(llm_daily.get(day, {"llm_sessions": 0}))
                merged.setdefault("llm_sessions", 0)
                store.record_day(history, day, "ga4", merged)

            snapshot["llm_by_source"] = llm_by_source
            source_status["ga4"] = {"ok": True, "days_pulled": len(sessions)}
        except Exception as exc:  # noqa: BLE001
            source_status["ga4"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- Core Web Vitals --------------------------------------------------
    # Weekly cadence. CrUX reports a 28-day trailing window, so daily polling
    # costs quota and tells you nothing new.
    last_cwv_run = history["state"].get("last_cwv_run")
    due = (
        last_cwv_run is None
        or (as_of - dt.date.fromisoformat(last_cwv_run)).days >= 7
    )
    if due:
        try:
            readings = {"origin": crux.fetch(f"https://{client_cfg.domain}", origin=True)}
            for label, url in client_cfg.template_samples.items():
                readings[label] = crux.fetch(url)

            readings = {k: v for k, v in readings.items() if v}
            if readings:
                snapshot["cwv"] = readings
                history["state"]["last_cwv_run"] = as_of.isoformat()
                source_status["crux"] = {"ok": True, "targets": len(readings)}
            else:
                source_status["crux"] = {
                    "ok": True,
                    "note": "no CrUX data (missing CRUX_API_KEY or insufficient traffic)",
                }
        except Exception as exc:  # noqa: BLE001
            source_status["crux"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        snapshot["cwv"] = history["state"].get("last_cwv") or {}

    snapshot["source_status"] = source_status
    return snapshot, source_status
