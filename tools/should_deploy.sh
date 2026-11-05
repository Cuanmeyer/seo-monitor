#!/usr/bin/env bash
# Netlify "ignore" command for this project.
#
# exit 0 = skip the deploy
# exit 1 = publish
#
# Each production deploy costs 15 Netlify credits. The Prism team is on the
# Personal plan (1,000 credits a month), so one deploy per daily run is about
# 450 credits a month and fits comfortably. The dashboard therefore publishes
# whenever the page itself changed, which in practice is once per daily run.
#
# The one thing still skipped: a commit that does not touch public/. Editing a
# template or clients.yaml does not re-render the page, the next monitor run
# does, so publishing that commit would serve the old page and waste a deploy.

set -u

say() { echo "[should_deploy] $*"; }

if [ -z "${CACHED_COMMIT_REF:-}" ] || [ -z "${COMMIT_REF:-}" ]; then
  say "no cached deploy to compare against, publishing"
  exit 1
fi

if git diff --quiet "$CACHED_COMMIT_REF" "$COMMIT_REF" -- public/ 2>/dev/null; then
  say "public/ is unchanged since the live deploy, skipping"
  exit 0
fi

say "dashboard changed, publishing"
exit 1
