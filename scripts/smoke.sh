#!/usr/bin/env bash
# Phase 1 smoke test: client -> APISIX -> MaaS (mode A).
# Usage: scripts/smoke.sh   (reads .env; APISIX_URL defaults to http://localhost:9080)
set -uo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "missing .env (copy .env.example)"; exit 2; }
set -a; . ./.env; set +a

BASE="${APISIX_URL:-http://localhost:9080}"
LOG_FILE="logs/apisix/requests.log"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }

IFS=',' read -r -a DEVS <<< "$DEV_KEYS"
DEV1_NAME="${DEVS[0]%%:*}"; DEV1_KEY="${DEVS[0]#*:}"
DEV2_NAME="${DEVS[1]%%:*}"; DEV2_KEY="${DEVS[1]#*:}"

post() { # post <outfile> <body> [curl header args...] -> prints http code
  local out="$1" body="$2"; shift 2
  curl -sS -o "$out" -w '%{http_code}' --max-time 600 -X POST "$BASE/v1/messages" \
    -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' "$@" --data "$body"
}

echo "== waiting for APISIX at $BASE"
for _ in $(seq 1 30); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")" != "000" ]] && break
  sleep 1
done

BODY_PING=$(printf '{"model":"%s","max_tokens":64,"messages":[{"role":"user","content":"Reply with the single word: pong"}]}' "$MAAS_MODEL_MAIN")
BODY_STREAM=$(printf '{"model":"%s","max_tokens":512,"stream":true,"messages":[{"role":"user","content":"Count from 1 to 40, one number per line, nothing else."}]}' "$MAAS_MODEL_MAIN")

has_usage() {
  python3 - "$1" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
u = d.get("usage")
print("    usage:", json.dumps(u))
sys.exit(0 if isinstance(u, dict) and "input_tokens" in u else 1)
EOF
}

echo "== 1. non-stream"
code=$(post "$TMP/1a" "$BODY_PING" -H "x-api-key: $DEV1_KEY")
if [[ "$code" == 200 ]] && has_usage "$TMP/1a"; then ok "1a x-api-key ($DEV1_NAME): 200 + usage"
else fail "1a x-api-key ($DEV1_NAME): http $code"; head -c 600 "$TMP/1a"; echo; fi

code=$(post "$TMP/1b" "$BODY_PING" -H "Authorization: Bearer $DEV2_KEY")
if [[ "$code" == 200 ]] && has_usage "$TMP/1b"; then ok "1b Authorization: Bearer ($DEV2_NAME): 200 + usage"
else fail "1b Authorization: Bearer ($DEV2_NAME): http $code"; head -c 600 "$TMP/1b"; echo; fi

echo "== 2. stream (SSE must arrive incrementally)"
if curl -sS -N --max-time 600 -X POST "$BASE/v1/messages" \
     -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
     -H "x-api-key: $DEV1_KEY" --data "$BODY_STREAM" \
     -w '\n__HTTP_STATUS__:%{http_code}\n' | python3 -u scripts/sse_timing.py; then
  ok "2 stream: all events, delivered incrementally"
else
  fail "2 stream"
fi

echo "== 3. auth"
code=$(post "$TMP/3a" "$BODY_PING" -H "x-api-key: wrong-key")
[[ "$code" == 401 ]] && ok "3a wrong x-api-key -> 401" || fail "3a wrong x-api-key -> $code"
code=$(post "$TMP/3b" "$BODY_PING" -H "Authorization: Bearer wrong-key")
[[ "$code" == 401 ]] && ok "3b wrong Bearer -> 401" || fail "3b wrong Bearer -> $code"
code=$(post "$TMP/3c" "$BODY_PING")
[[ "$code" == 401 ]] && ok "3c missing key -> 401" || fail "3c missing key -> $code"

echo "== 4. secrets not in logs"
sleep 1
if [[ -z "${MAAS_API_KEY:-}" ]]; then
  fail "4 MAAS_API_KEY empty, cannot check"
else
  docker compose logs --no-color apisix apisix-render > "$TMP/compose.log" 2>&1
  leaks=0
  for secret in "$MAAS_API_KEY" "$DEV1_KEY" "$DEV2_KEY"; do
    if grep -rqF -- "$secret" logs/ "$TMP/compose.log"; then leaks=1; fi
  done
  [[ $leaks == 0 ]] && ok "4a MAAS_API_KEY and dev keys absent from logs/ and container logs" \
                    || fail "4a a key was found in logs/ or container logs"
fi

if [[ -s "$LOG_FILE" ]] && python3 - "$LOG_FILE" "$DEV1_NAME" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
mine = [r for r in rows if r.get("consumer") == sys.argv[2] and r.get("status") == "200"]
print(f"    {len(rows)} log lines, {len(mine)} ok lines for {sys.argv[2]}")
if not mine:
    sys.exit(1)
r = mine[-1]
print("    last:", {k: r.get(k) for k in ("consumer", "status", "request_time", "upstream_uri", "request_length")},
      "request_body bytes:", len(r.get("request_body") or ""))
forbidden = {"authorization", "x-api-key", "headers"}
sys.exit(0 if r.get("request_body") and not forbidden & {k.lower() for k in r} else 1)
EOF
then ok "4b file-logger: consumer/status/latency/body logged, no header fields"
else fail "4b file-logger output missing or incomplete ($LOG_FILE)"; fi

echo
echo "passed=$PASS failed=$FAIL"
[[ $FAIL == 0 ]]
