#!/usr/bin/env bash
# Netlify "ignore" command for this project.
#
#   exit 0  = skip the deploy
#   exit 1  = publish
#
# Netlify's free plan bills a flat 15 credits per production deploy against a
# 300 credit monthly allowance, which is 20 deploys a month and nothing else.
# Build time, bandwidth and requests here are all rounding errors. A daily
# monitor run needs about 30 deploys, so publishing on every run cannot fit.
#
# So publish when it is worth publishing: whenever the alert picture actually
# changes, whenever the code or config changes, and once a week regardless so
# the figures never go properly stale. Everything else is skipped. The run
# itself still happens daily and still commits its data; only the publish is
# rationed.
#
# Note on the comparison: data/alerts.json is rewritten on every run even when
# nothing meaningful changed, because open alerts carry a last_seen timestamp
# and an occurrence counter that tick each time. Diffing the file directly
# would therefore publish every day and save nothing. What matters is the set
# of client / signal / severity / status rows, so that is what gets compared.

set -u

say() { echo "[should_deploy] $*"; }

# --- weekly refresh ---------------------------------------------------
if [ "$(date -u +%u)" = "1" ]; then
  say "Monday: weekly refresh"
  exit 1
fi

# --- first build, or no cached deploy to compare against --------------
if [ -z "${CACHED_COMMIT_REF:-}" ] || [ -z "${COMMIT_REF:-}" ]; then
  say "no cached deploy to compare against, publishing"
  exit 1
fi

# --- code or config changed -------------------------------------------
if ! git diff --quiet "$CACHED_COMMIT_REF" "$COMMIT_REF" -- \
     monitor/ clients.yaml netlify.toml run.py 2>/dev/null; then
  say "code or config changed, publishing"
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
