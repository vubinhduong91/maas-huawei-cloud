#!/usr/bin/env bash
# Hub-fix Phase 4: run the hand-over files themselves on real APISIX, upstream = the replay echo.
#   0. deliverables/hubfix is what scenarios/hubfix_deliverables.py generates from patch.lua now.
#   1. route.apisix.yaml on a fresh APISIX 3.11.0 standalone with a MINIMAL config.yaml (default
#      body buffers, as a production gateway may have): the 6 Sonnet replay bodies (small/medium/
#      large x effort medium/xhigh) and the ~160K-token body, each must reach the echo as exactly
#      the body P4 must send (same bytes as reference = n).
#   2. route.admin.json PUT to the dashboard APISIX 3.14.1 (Admin API + etcd): schema accepted,
#      then the same check through it (route copy with hosts ["hubfix.test"] so it does not clash
#      with the lab routes mirrored there).
# Only test containers and the echo are touched: the lab gateway and the daily routes are not.
# Usage: scenarios/hubfix_deliverable_test.sh
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LAB"
export LAB_UID="$(id -u)" LAB_GID="$(id -g)"
OUT="$LAB/logs/hubfix/deliverable-test"
BODIES="$LAB/logs/replay/bodies-hubfix"
OBODIES="$LAB/logs/replay/bodies-hubfix-overhead"
NET=maas-cache-lab_default
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
WINDOWS="$LAB/logs/replay/windows-deliverable-$RUN_ID.jsonl"
FAIL=0
rm -rf "$OUT" && mkdir -p "$OUT"

echo "== 0. deliverables/hubfix is up to date"
python3 scenarios/hubfix_deliverables.py --out "$OUT/current" >/dev/null
if diff -r "$OUT/current" deliverables/hubfix --exclude '*.md' >/dev/null; then echo "   ok"; else
  echo "   FAIL: deliverables/hubfix differs from the generator output (run scenarios/hubfix_deliverables.py)"; FAIL=1; fi
python3 scenarios/hubfix_deliverables.py --out "$OUT/echo" --upstream-host replay-echo --upstream-port 9099 \
  --upstream-scheme http --path-prefix /anthropic --auth "Bearer deliverable-test" >/dev/null

cat > "$OUT/config.yaml" <<'EOF'
apisix:
  node_listen:
    - 9080
  enable_admin: false
deployment:
  role: data_plane
  role_data_plane:
    config_provider: yaml
EOF

cleanup() {
  docker rm -f hubfix-deliverable-311 >/dev/null 2>&1
  [[ -n "${DASH_STARTED:-}" ]] && make -s dashboard-down >/dev/null 2>&1
  docker compose --profile keyorder stop replay-echo >/dev/null 2>&1
}
trap cleanup EXIT
[[ -f logs/replay/echo.jsonl ]] && mv logs/replay/echo.jsonl "logs/replay/echo-before-deliverable-$RUN_ID.jsonl"
docker compose --profile keyorder up -d --force-recreate replay-echo >/dev/null 2>&1

check() {   # tag-prefix: every body sent under it must equal its expected bytes
  local prefix="$1" ok=1
  for spec in "${CASES[@]}"; do
    local name="${spec%%:*}" ref="${spec#*:}"
    local line
    line="$(python3 scenarios/replay/replay.py report --log logs/replay/echo.jsonl --windows "$WINDOWS" \
              --reference "$ref" | grep -F "| $prefix/$name |")"
    local n same
    n="$(echo "$line" | awk -F'|' '{gsub(/ /,"",$3); print $3}')"
    same="$(echo "$line" | awk -F'|' '{gsub(/ /,"",$5); print $5}')"
    echo "   $prefix/$name: $same/$n same bytes as the expected P4 body"
    [[ -n "$n" && "$n" == "$same" && "$n" != 0 ]] || ok=0
  done
  [[ $ok == 1 ]] || { echo "   FAIL: $prefix"; FAIL=1; }
}

CASES=()
for b in small medium large; do for l in medium xhigh; do
  CASES+=("$b-$l:$BODIES/$b-$l.expected.json"); done; done
CASES+=("160k:$OBODIES/160k.expected.json")
body_of() { case "$1" in 160k) echo "$OBODIES/160k.json" ;; *) echo "$BODIES/$1.json" ;; esac; }

echo "== 1. route.apisix.yaml on APISIX 3.11.0 standalone (minimal config.yaml)"
docker rm -f hubfix-deliverable-311 >/dev/null 2>&1
docker run -d --name hubfix-deliverable-311 --network "$NET" -p 127.0.0.1:9380:9080 \
  -v "$OUT/config.yaml:/usr/local/apisix/conf/config.yaml:ro" \
  -v "$OUT/echo/route.apisix.yaml:/usr/local/apisix/conf/apisix.yaml:ro" \
  apache/apisix:3.11.0-debian >/dev/null
for _ in $(seq 30); do [[ "$(curl -s -o /dev/null -w '%{http_code}' localhost:9380/)" != 000 ]] && break; sleep 1; done
for spec in "${CASES[@]}"; do
  name="${spec%%:*}"
  python3 scenarios/replay/replay.py send --base-url "http://127.0.0.1:9380/v1/messages?beta=true" \
    --body-file "$(body_of "$name")" -n 10 --concurrency 4 --tag "yaml311/$name" --windows "$WINDOWS" >/dev/null
done
check yaml311
docker logs hubfix-deliverable-311 2>&1 | grep -iE "\[error\]|hubfix" | head -3

echo "== 2. route.admin.json via the Admin API of APISIX 3.14.1 (dashboard)"
make -s dashboard >/dev/null 2>&1 && DASH_STARTED=1
KEY="$(sed -n 's/^DASHBOARD_ADMIN_KEY=//p' .env)"
python3 - "$OUT/echo/route.admin.json" "$OUT/admin-test.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
r["hosts"] = ["hubfix.test"]
json.dump(r, open(sys.argv[2], "w"))
EOF
for _ in $(seq 30); do [[ "$(curl -s -o /dev/null -w '%{http_code}' localhost:9180/apisix/admin/routes -H "X-API-KEY: $KEY")" == 200 ]] && break; sleep 2; done
code="$(curl -s -o "$OUT/admin-put.out" -w '%{http_code}' -X PUT localhost:9180/apisix/admin/routes/hubfix-deliverable-test \
          -H "X-API-KEY: $KEY" -H 'Content-Type: application/json' --data-binary @"$OUT/admin-test.json")"
echo "   PUT route: HTTP $code"
[[ "$code" == 200 || "$code" == 201 ]] || { cat "$OUT/admin-put.out"; FAIL=1; }
sleep 2
for spec in "${CASES[@]}"; do
  name="${spec%%:*}"
  rel="$(body_of "$name")"; rel="${rel#"$LAB"/logs/replay/}"
  docker run --rm --network "$NET" -v "$LAB/scenarios/replay:/app:ro" -v "$LAB/logs/replay:/data" python:3.11-slim \
    python /app/replay.py send --base-url "http://maas-cache-lab-apisix-dashboard-1:9080/v1/messages?beta=true" \
    --header "Host: hubfix.test" --body-file "/data/$rel" -n 10 --concurrency 4 \
    --tag "admin3141/$name" --windows "/data/$(basename "$WINDOWS")" >/dev/null 2>&1
done
check admin3141
curl -s -o /dev/null -X DELETE localhost:9180/apisix/admin/routes/hubfix-deliverable-test -H "X-API-KEY: $KEY"

echo "== result: $([[ $FAIL == 0 ]] && echo PASS || echo FAIL)"
exit $FAIL
