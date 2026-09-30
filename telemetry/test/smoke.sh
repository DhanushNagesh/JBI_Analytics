#!/usr/bin/env bash
# Smoke test against a running Worker: ./test/smoke.sh [base_url] [token]
set -u
BASE=${1:-http://localhost:8787}
TOKEN=${2:-local-dev-token}
NOW=$(date +%s)
JOB="smoke-$NOW"
BATCH=$(cat <<JSON
[
 {"event":"session_start","level":"INFO","jobId":"$JOB","placeVersion":42,"at":$((NOW-120)),"uptime":1,
  "userId":111,"sessionId":"S1-$NOW","joinedAt":$((NOW-120)),"accountAgeDays":400,"followed":false,"private":false,"studio":false},
 {"event":"session_start","level":"INFO","jobId":"$JOB","placeVersion":42,"at":$((NOW-90)),"uptime":30,
  "userId":222,"sessionId":"S2-$NOW","joinedAt":$((NOW-90)),"accountAgeDays":9,"followed":true,"private":false,"studio":false},
 {"event":"round_start","level":"INFO","jobId":"$JOB","placeVersion":42,"at":$((NOW-60)),"uptime":60,"roundId":"R-$NOW","map":"Mall"},
 {"event":"shot","level":"INFO","jobId":"$JOB","placeVersion":42,"at":$((NOW-50)),"uptime":70},
 {"event":"session_end","level":"INFO","jobId":"$JOB","placeVersion":42,"at":$((NOW-30)),"uptime":90,
  "userId":222,"sessionId":"S2-$NOW","joinedAt":$((NOW-90)),"leftAt":$((NOW-30)),"seconds":60,
  "afkSeconds":0,"practiceSeconds":0,"roundSeconds":30,"rounds":1,"reason":"left"},
 {"event":"presence","level":"INFO","jobId":"$JOB","placeVersion":42,"at":$((NOW-10)),"uptime":110,
  "maxPlayers":11,"private":false,"studio":false,"players":[{"u":111,"s":"S1-$NOW","t":$((NOW-120)),"st":"hider"}]}
]
JSON
)
post() { curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/ingest" -H "content-type: application/json" "$@"; }
echo "bad token:   $(post -H 'authorization: Bearer nope' --data "$BATCH")"
echo "no token:    $(post --data "$BATCH")"
echo "not array:   $(post -H "authorization: Bearer $TOKEN" --data '{"a":1}')"
echo "good batch:  $(post -H "authorization: Bearer $TOKEN" --data "$BATCH")"
echo "replay:      $(post -H "authorization: Bearer $TOKEN" --data "$BATCH")"
echo "live:        $(curl -s "$BASE/api/live")"
echo "summary:     $(curl -s "$BASE/api/summary?from=$((NOW-3600))&to=$NOW")"
echo "top:         $(curl -s "$BASE/api/top?from=$((NOW-3600))&to=$NOW")"
echo "rounds:      $(curl -s "$BASE/api/rounds?from=$((NOW-3600))&to=$NOW")"
echo "badge ccu:   $(curl -s "$BASE/badge/ccu")"
echo "badge time:  $(curl -s "$BASE/badge/playtime")"
echo "badge bad:   $(curl -s -o /dev/null -w "%{http_code}" "$BASE/badge/nope")"
