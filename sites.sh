#!/usr/bin/env bash
#
# sites.sh — list the sites attempted in a crawl run.
#
# Usage:
#   ./sites.sh            # sites from the most recent run
#   ./sites.sh <logfile>  # sites from a specific logs/crawl-*.log
#
# Requires NOISY_CRAWL_LOG_LEVEL=debug (the default) when the run happened,
# since the "Visiting" lines are only emitted at debug level.
#
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

LOG="${1:-$(ls -t "$HERE"/logs/crawl-*.log 2>/dev/null | head -1)}"
if [ -z "${LOG:-}" ] || [ ! -f "$LOG" ]; then
    echo "No crawl log found. Has a run completed yet? (looked in $HERE/logs)" >&2
    exit 1
fi

echo "# Sites attempted in: $LOG" >&2
# Pull the URL out of each "Visiting <url> (ref: ...)" line, dedupe, count.
grep -oE 'Visiting [^ ]+' "$LOG" | awk '{print $2}' | sort -u
COUNT=$(grep -oE 'Visiting [^ ]+' "$LOG" | awk '{print $2}' | sort -u | wc -l | tr -d ' ')
echo "# unique URLs: $COUNT" >&2
