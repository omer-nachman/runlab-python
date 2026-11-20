#!/usr/bin/env bash
# The same loop over plain HTTP: plan, record 5 seeds per arm, compare.
# Needs RUNLAB_URL (e.g. https://runlab.example.com) and RUNLAB_TOKEN; bash, curl.
set -euo pipefail
: "${RUNLAB_URL:?set RUNLAB_URL}" "${RUNLAB_TOKEN:?set RUNLAB_TOKEN}"

api() {   # api METHOD PATH [JSON]: one call; prints the JSON response, fails on an HTTP error
  curl -sS --fail-with-body -X "$1" "$RUNLAB_URL/api/v1$2" \
    -H "Authorization: Bearer $RUNLAB_TOKEN" -H 'Content-Type: application/json' \
    ${3:+--data "$3"}
  echo
}

api GET /me                                    # {"name": ..., "role": ...}

api POST /research/plans '{"project": "curl-demo", "a": "treatment", "b": "baseline",
  "metric": "acc", "hypothesis": "the change raises accuracy", "n_max": 5,
  "stopping": "fixed"}'

# One RESULT line per arm and seed, joined by \n (a JSON newline escape).
lines=""
for s in 0 1 2 3 4; do
  lines+="RESULT: treatment acc=0.8$((2 + s % 2))$s seed=$s\\nRESULT: baseline acc=0.80$s seed=$s\\n"
done
api POST /research/results "{\"project\": \"curl-demo\", \"results\": \"$lines\"}"

api POST /research/compare '{"project": "curl-demo", "a": "treatment", "b": "baseline",
  "metric": "acc"}'
