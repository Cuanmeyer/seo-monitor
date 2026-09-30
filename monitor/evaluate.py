"""Threshold evaluation.

The comparison throughout is a 7-day current window against the prior 28 days
divided by 4. Twenty-eight days is exactly four of each weekday, so that
average is weekday-aligned by construction: no Monday is ever compared against
a Saturday, which is where naive period-over-period monitors generate most of
their false alarms.

Nothing alerts on its first breach. A signal has to stay outside threshold for
a configured number of consecutive runs before it leaves the building, and the
streak counter lives in the history file so it survives between runs.
"""

from __future__ import annotations

import datetime as dt

from . import store


class Finding:
    def __init__(
        self,
        signal: str,
        severity: str,
        summary: str,
        current_value,
        baseline_value,
        pct_change,
        detail: list[str] | None = None,
        hint: str | None = None,
        link: str | None = None,
    ):
        self.signal = signal
        self.severity = severity
        self.summary = summary
        self.current_value = current_value
        self.baseline_value = baseline_value
        self.pct_change = pct_change
        self.detail = detail or []
        self.hint = hint
        self.link = link

    def to_alert(self, client_slug: str, client_name: str, as_of: str) -> dict:
        return {
            "client": client_slug,
            "client_name": client_name,
            "signal": self.signal,
            "severity": self.severity,
            "summary": self.summary,
            "current_value": self.current_value,
            "baseline_value": self.baseline_value,
            "pct_change": self.pct_change,
            "detail": self.detail,
            "hint": self.hint,
            "link": self.link,
            "as_of": as_of,
            "detected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }


def pct_change(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return round((current - baseline) / baseline * 100, 1)


def gsc_link(client_cfg, report: str = "performance") -> str:
    from urllib.parse import quote

    prop = quote(client_cfg.gsc_property, safe="")
    if report == "indexing":
        return f"https://search.google.com/search-console/index?resource_id={prop}"
    return f"https://search.google.com/search-console/performance/search-analytics?resource_id={prop}"


def _windows(client_cfg, history, source: str, metric: str, as_of: dt.date):
    """Current 7-day total and the weekday-aligned baseline for the same length.

    Returns None when either window is incomplete, because a missing day would
    otherwise read as a drop that never happened.
    """
    current_days = client_cfg.setting("current_window_days", 7)
    baseline_days = client_cfg.setting("baseline_window_days", 28)

    data = store.series(history, source, metric)
    current, current_found = store.window_sum(data, as_of, current_days)
    baseline_end = as_of - dt.timedelta(days=current_days)
    baseline_total, baseline_found = store.window_sum(data, baseline_end, baseline_days)

    if current_found < current_days or baseline_found < baseline_days:
        return None

    baseline = baseline_total / (baseline_days / current_days)
    return current, baseline


def _bump_streak(history: dict, key: str, breached: bool, as_of: dt.date) -> int:
    """Advance the persistence counter, once per evaluated date.

    Re-running the same day (a manual trigger, an Actions retry, a backfill
    overlapping today) must not advance persistence, or a single day's breach
    could be replayed into a three-day streak and alert on nothing.
    """
    streaks = history["state"].setdefault("streaks", {})
    seen = history["state"].setdefault("streak_dates", {})
    day = as_of.isoformat()

    if seen.get(key) == day:
        return streaks.get(key, 0)

    seen[key] = day
    streaks[key] = (streaks.get(key, 0) + 1) if breached else 0
    return streaks[key]


def _top_movers(snapshot: dict, metric: str = "clicks", limit: int = 5) -> list[str]:
    """Pages that lost the most, current window against the baseline average."""
    current = snapshot.get("pages_current") or {}
    baseline = snapshot.get("pages_baseline") or {}
    scale = snapshot.get("baseline_scale", 4)

    moves = []
    for url, values in current.items():
        now = values.get(metric, 0)
        before = (baseline.get(url, {}).get(metric, 0)) / scale
        if before < 5:
            continue
        moves.append((now - before, url, now, before))

    moves.sort(key=lambda row: row[0])
    out = []
    for delta, url, now, before in moves[:limit]:
        if delta >= 0:
            break
        change = pct_change(now, before)
        path = url.replace(f"https://{snapshot.get('domain', '')}", "") or "/"
        out.append(f"{path}: {int(before)} -> {int(now)} ({change:+.0f}%)")
    return out


def _cause_hint(findings: list[Finding], clicks_pct, impressions_pct, position_delta) -> str:
    signals = {f.signal for f in findings}

    if "indexation_priority" in signals or "indexation_rate" in signals:
        return "Indexation changed in the same run. Check coverage before looking anywhere else."
    if "core_web_vitals" in signals:
        return "Core Web Vitals regressed in the same run. Check for a recent deploy on the affected template."

    if clicks_pct is not None and impressions_pct is not None:
        if clicks_pct <= -15 and impressions_pct > -8:
            return (
                "Impressions held while clicks fell, so ranking is probably intact. "
                "Look at SERP appearance, title rewrites or a new feature taking the click."
            )
        if clicks_pct <= -15 and impressions_pct <= -15:
            return (
                "Clicks and impressions fell together, which is visibility loss rather than "
                "a CTR problem. Check indexation and rankings on the affected pages."
            )
        if impressions_pct >= 20 and abs(clicks_pct) < 8:
            base = (
                "Impressions expanded without clicks following, which usually means new "
                "exposure on lower-relevance queries rather than a genuine gain."
            )
            if position_delta and position_delta > 0.3:
                base += f" Average position also worsened by {position_delta:.1f}, which supports that reading."
            return base
    return "No single cause stands out. Open the GSC performance view and segment by page and query."


def evaluate_client(client_cfg, history: dict, snapshot: dict, as_of: dt.date,
                    source_status: dict) -> tuple[list[Finding], set[str], set[str]]:
    """Returns findings, signals confirmed inside threshold, and signals that
    are currently breaching but have not yet met their persistence bar.

    That third set matters. Without it a signal on day one of a three-day
    breach is indistinguishable from a signal with no data at all, which is
    exactly the state someone checking the dashboard most needs to see.
    """
    findings: list[Finding] = []
    healthy: set[str] = set()
    watching: set[str] = set()

    # A source that failed must never read as "all green". Raise it loudly and
    # skip the signals that depend on it rather than evaluating against a gap.
    for source, status in source_status.items():
        if not status.get("ok"):
            findings.append(
                Finding(
                    signal=f"monitor_failure_{source}",
                    severity="critical",
                    summary=f"{source.upper()} collection failed, signals from it are not being checked",
                    current_value=None,
                    baseline_value=None,
                    pct_change=None,
                    detail=[status.get("error", "unknown error")],
                    hint="A silent gap is worse than a red light. Fix the collector before trusting this client's status.",
                )
            )

    min_clicks = client_cfg.setting("min_baseline_clicks_per_week", 100)

    clicks_pct = impressions_pct = None
    position_delta = None

    # --- Organic clicks and impressions -----------------------------------
    if source_status.get("gsc", {}).get("ok"):
        clicks = _windows(client_cfg, history, "gsc", "clicks", as_of)
        impressions = _windows(client_cfg, history, "gsc", "impressions", as_of)

        if clicks and clicks[1] >= min_clicks:
            current, baseline = clicks
            clicks_pct = pct_change(current, baseline)
            warn = client_cfg.threshold("organic_clicks", "warning_pct", -20)
            crit = client_cfg.threshold("organic_clicks", "critical_pct", -35)
            persist = client_cfg.threshold("organic_clicks", "persist_runs", 3)

            severity = None
            if clicks_pct is not None and clicks_pct <= crit:
                severity = "critical"
            elif clicks_pct is not None and clicks_pct <= warn:
                severity = "warning"

            streak = _bump_streak(history, "organic_clicks", severity is not None, as_of)
            if severity and streak >= persist:
                findings.append(
                    Finding(
                        signal="organic_clicks",
                        severity=severity,
                        summary=f"Organic clicks {clicks_pct:+.1f}% against a 28-day baseline",
                        current_value=int(current),
                        baseline_value=int(baseline),
                        pct_change=clicks_pct,
                        detail=_top_movers(snapshot, "clicks"),
                        link=gsc_link(client_cfg),
                    )
                )
            elif not severity:
                healthy.add("organic_clicks")
            else:
                watching.add("organic_clicks")

        if impressions and impressions[1] >= min_clicks:
            current, baseline = impressions
            impressions_pct = pct_change(current, baseline)
            warn = client_cfg.threshold("organic_impressions", "warning_pct", -20)
            crit = client_cfg.threshold("organic_impressions", "critical_pct", -35)
            persist = client_cfg.threshold("organic_impressions", "persist_runs", 3)

            severity = None
            if impressions_pct is not None and impressions_pct <= crit:
                severity = "critical"
            elif impressions_pct is not None and impressions_pct <= warn:
                severity = "warning"

            streak = _bump_streak(history, "organic_impressions", severity is not None, as_of)
            if severity and streak >= persist:
                findings.append(
                    Finding(
                        signal="organic_impressions",
                        severity=severity,
                        summary=f"Organic impressions {impressions_pct:+.1f}% against a 28-day baseline",
                        current_value=int(current),
                        baseline_value=int(baseline),
                        pct_change=impressions_pct,
                        detail=_top_movers(snapshot, "impressions"),
                        link=gsc_link(client_cfg),
                    )
                )
            elif not severity:
                healthy.add("organic_impressions")
            else:
                watching.add("organic_impressions")

        # --- CTR divergence -----------------------------------------------
        # Not in the original brief. Added because a clicks-only threshold
        # sleeps through the case where impressions climb sharply, clicks stay
        # flat and average position degrades. That pattern is real and worth
        # knowing about: it is what an AI Overview or a broad category
        # expansion looks like from the outside.
        position_data = store.series(history, "gsc", "position")
        current_days = client_cfg.setting("current_window_days", 7)
        pos_now, n1 = store.window_sum(position_data, as_of, current_days)
        pos_before, n2 = store.window_sum(
            position_data, as_of - dt.timedelta(days=current_days), 28
        )
        if n1 == current_days and n2 == 28:
            position_delta = round((pos_now / n1) - (pos_before / n2), 2)

        if clicks_pct is not None and impressions_pct is not None:
            # Test CTR directly rather than inferring it from a pair of
            # separate conditions on clicks and impressions. Two thresholds
            # joined by AND let a real divergence slip through on a
            # technicality when one leg misses its cut by a point or two.
            ctr_change = pct_change(1 + clicks_pct / 100, 1 + impressions_pct / 100)
            ctr_limit = client_cfg.threshold("ctr_divergence", "warning_ctr_drop_pct", -20)
            holding = client_cfg.threshold(
                "ctr_divergence", "impressions_holding_above_pct", -5
            )
            persist = client_cfg.threshold("ctr_divergence", "persist_runs", 2)

            # Only meaningful while impressions hold up. If impressions are
            # falling too, this is a visibility problem and the clicks and
            # impressions signals already own it.
            breached = (
                ctr_change is not None
                and ctr_change <= ctr_limit
                and impressions_pct >= holding
            )
            streak = _bump_streak(history, "ctr_divergence", breached, as_of)
            if breached and streak >= persist:
                findings.append(
                    Finding(
                        signal="ctr_divergence",
                        severity="warning",
                        summary=(
                            f"CTR down {abs(ctr_change):.0f}% while impressions ran "
                            f"{impressions_pct:+.1f}% and clicks {clicks_pct:+.1f}%"
                        ),
                        current_value=round(ctr_change, 1),
                        baseline_value=0,
                        pct_change=ctr_change,
                        detail=(
                            [f"Average position moved {position_delta:+.2f} over the same window"]
                            if position_delta is not None
                            else []
                        ),
                        link=gsc_link(client_cfg),
                    )
                )
            elif not breached:
                healthy.add("ctr_divergence")
            else:
                watching.add("ctr_divergence")

    # --- Indexation -------------------------------------------------------
    inspections = snapshot.get("inspections") or {}
    if inspections:
        broken = []
        for url, result in inspections.items():
            if not result.get("ok"):
                continue
            if result.get("verdict") != "PASS":
                broken.append(f"{url} -> {result.get('coverage_state') or result.get('verdict')}")
            elif result.get("google_canonical") and result.get("user_canonical"):
                if result["google_canonical"] != result["user_canonical"]:
                    broken.append(f"{url} -> canonical mismatch")

        streak = _bump_streak(history, "indexation_priority", bool(broken), as_of)
        persist = client_cfg.threshold("indexation_priority", "persist_runs", 1)
        if broken and streak >= persist:
            findings.append(
                Finding(
                    signal="indexation_priority",
                    severity="critical",
                    summary=f"{len(broken)} priority URL(s) not indexed as expected",
                    current_value=len(broken),
                    baseline_value=0,
                    pct_change=None,
                    detail=broken[:5],
                    link=gsc_link(client_cfg, "indexing"),
                )
            )
        elif not broken:
            healthy.add("indexation_priority")
        else:
            watching.add("indexation_priority")

        rate = snapshot.get("indexed_rate")
        previous = history["state"].get("last_indexed_rate")
        if rate is not None:
            if previous is not None:
                drop = previous - rate
                limit = client_cfg.threshold("indexation_rate", "warning_drop_points", 5)
                persist = client_cfg.threshold("indexation_rate", "persist_runs", 2)
                streak = _bump_streak(history, "indexation_rate", drop >= limit, as_of)
                if drop >= limit and streak >= persist:
                    findings.append(
                        Finding(
                            signal="indexation_rate",
                            severity="warning",
                            summary=f"Indexed rate on the sample fell {drop:.1f} points to {rate:.1f}%",
                            current_value=rate,
                            baseline_value=previous,
                            pct_change=pct_change(rate, previous),
                            link=gsc_link(client_cfg, "indexing"),
                        )
                    )
                elif drop < limit:
                    healthy.add("indexation_rate")
                else:
                    watching.add("indexation_rate")
            history["state"]["last_indexed_rate"] = rate

    # --- Core Web Vitals --------------------------------------------------
    cwv_now = snapshot.get("cwv") or {}
    cwv_before = history["state"].get("last_cwv") or {}
    regressions = []
    for label, record in cwv_now.items():
        if not record or not record.get("ok"):
            continue
        prior = cwv_before.get(label) or {}
        for metric, values in record.get("metrics", {}).items():
            was = (prior.get("metrics") or {}).get(metric)
            if not was:
                continue
            if was["rating"] == "good" and values["rating"] != "good":
                regressions.append(
                    f"{label} {metric}: good -> {values['rating'].replace('_', ' ')} (p75 {values['p75']})"
                )
                continue
            if was.get("good_pct") and values.get("good_pct"):
                drop = was["good_pct"] - values["good_pct"]
                limit = client_cfg.threshold("core_web_vitals", "warning_good_drop_points", 10)
                if drop >= limit:
                    regressions.append(
                        f"{label} {metric}: good share -{drop:.0f} points to {values['good_pct']:.0f}%"
                    )

    if cwv_now:
        streak = _bump_streak(history, "core_web_vitals", bool(regressions), as_of)
        persist = client_cfg.threshold("core_web_vitals", "persist_runs", 1)
        if regressions and streak >= persist:
            findings.append(
                Finding(
                    signal="core_web_vitals",
                    severity="warning",
                    summary=f"Core Web Vitals regressed on {len(regressions)} template metric(s)",
                    current_value=len(regressions),
                    baseline_value=0,
                    pct_change=None,
                    detail=regressions[:5],
                    hint="CrUX is a 28-day trailing window, so the underlying change started earlier than today.",
                )
            )
        elif not regressions:
            healthy.add("core_web_vitals")
        else:
            watching.add("core_web_vitals")
        history["state"]["last_cwv"] = cwv_now

    # --- LLM referrals ----------------------------------------------------
    if source_status.get("ga4", {}).get("ok"):
        llm = _windows(client_cfg, history, "ga4", "llm_sessions", as_of)
        if llm and llm[1] >= 20:
            current, baseline = llm
            change = pct_change(current, baseline)
            limit = client_cfg.threshold("llm_referrals", "warning_pct", -25)
            persist = client_cfg.threshold("llm_referrals", "persist_runs", 1)
            breached = change is not None and change <= limit
            streak = _bump_streak(history, "llm_referrals", breached, as_of)
            if breached and streak >= persist:
                findings.append(
                    Finding(
                        signal="llm_referrals",
                        severity="warning",
                        summary=f"Sessions from LLM surfaces {change:+.1f}% against baseline",
                        current_value=int(current),
                        baseline_value=int(baseline),
                        pct_change=change,
                        detail=[
                            f"{source}: {count}"
                            for source, count in sorted(
                                (snapshot.get("llm_by_source") or {}).items(),
                                key=lambda kv: -kv[1],
                            )[:5]
                        ],
                    )
                )
            elif not breached:
                healthy.add("llm_referrals")
            else:
                watching.add("llm_referrals")

    # Attach a shared cause hint to any finding that does not carry its own.
    hint = _cause_hint(findings, clicks_pct, impressions_pct, position_delta)
    for finding in findings:
        if not finding.hint and not finding.signal.startswith("monitor_failure"):
            finding.hint = hint

    return findings, healthy, watching
