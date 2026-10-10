#!/usr/bin/env bash
# Netlify "ignore" command for this project.
#
# exit 0 = skip the deploy
# exit 1 = publish
#
# Netlify's free plan bills a flat 15 credits per production deploy against a
# 300 credit monthly allowance, which is 20 deploys a month and nothing else.
# Build time, bandwidth and requests here are all rounding errors. A daily
# monitor run needs about 30 deploys, so publishing on every run cannot fit.
#
# So publish when it is worth publishing: whenever the alert picture actually
# changes, whenever the code or config behind the dashboard changes, and once
# a week regardless so the figures never go properly stale. Everything else
# is skipped. The run itself still happens daily and still commits its data;
# only the publish is rationed.
#
# Two things this has to get right, both learned the hard way:
#
# 1. A commit that does not touch public/ has nothing new to serve. Editing a
#    template does not re-render the dashboard, the next monitor run does, so
#    publishing the edit itself would serve the old page AND burn the credits
#    that the real render then cannot claim. Those commits are skipped, and
#    the following monitor run publishes instead. The practical effect is that
#    a code change reaches the site on the next run rather than immediately.
#
# 2. data/alerts.json is rewritten on every run even when nothing meaningful
#    changed, because open alerts carry a last_seen timestamp and an
#    occurrence counter that tick each time. Diffing the file directly would
#    publish every day and save nothing. What matters is the set of client /
#    signal / severity / status rows, so that is what gets compared.

set -u

say() { echo "[should_deploy] $*"; }

# --- first build, or no cached deploy to compare against --------------
if [ -z "${CACHED_COMMIT_REF:-}" ] || [ -z "${COMMIT_REF:-}" ]; then
  say "no cached deploy to compare against, publishing"
  exit 1
fi

# --- nothing the visitor can see has changed --------------------------
# Checked before everything else, including the weekly refresh: there is no
# point refreshing a page that is byte for byte what is already live.
if git diff --quiet "$CACHED_COMMIT_REF" "$COMMIT_REF" -- public/ 2>/dev/null; then
  say "public/ is unchanged since the live deploy, skipping"
  exit 0
fi

# --- weekly refresh ---------------------------------------------------
if [ "$(date -u +%u)" = "1" ]; then
  say "Monday: weekly refresh"
  exit 1
fi

# --- code or config changed since whatever is live --------------------
# This is what carries a template or threshold change onto the site. The
# comparison is against the commit Netlify last published, not the previous
# commit, so an edit made days ago still counts until it has been served.
if ! git diff --quiet "$CACHED_COMMIT_REF" "$COMMIT_REF" -- \
     monitor/ clients.yaml netlify.toml run.py 2>/dev/null; then
  say "code or config changed since the live deploy, publishing"
  exit 1
fi

# --- did the alert picture change? ------------------------------------
fingerprint() {
  git show "$1:data/alerts.json" 2>/dev/null | python3 -c '
import json, sys

try:
    alerts = json.load(sys.stdin)
except Exception:
    print("UNREADABLE")
    sys.exit(0)

rows = sorted(
    "{}|{}|{}|{}".format(
        a.get("client", ""), a.get("signal", ""),
        a.get("severity", ""), a.get("status", ""),
    )
    for a in alerts
)
print("\n".join(rows) if rows else "NONE")
'
}

before=$(fingerprint "$CACHED_COMMIT_REF")
after=$(fingerprint "$COMMIT_REF")

if [ "$before" = "UNREADABLE" ] || [ "$after" = "UNREADABLE" ]; then
  say "could not read the alert log, publishing to be safe"
  exit 1
fi

if [ "$before" != "$after" ]; then
  say "alert state changed, publishing"
  exit 1
fi

say "no alert, code or config change, skipping deploy"
exit 0
