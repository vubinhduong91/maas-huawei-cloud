#!/usr/bin/env bash
# Phase 2 checks against real MaaS. Run after `make up MODE=B` (APISIX -> tap, tap :9091 on).
#   1. mode B stream: SSE still incremental through APISIX + tap; tap record complete
#   2. large body (~BIG_KB KiB, default 600 ≈ 150K tokens) sent twice through APISIX:
#      APISIX log body == tap body; second send shows whether MaaS reuses the prefix
#   3. mode C (client -> tap -> MaaS): stream OK, dev key swapped, wrong key 401
#   4. client aborts mid-stream -> tap records 499
#   5. scripts/diff_bodies.py: every APISIX body == tap body, headers unchanged
#   6. no MaaS/dev key in logs/tap
set -uo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "missing .env"; exit 2; }
set -a; . ./.env; set +a

BASE="${APISIX_URL:-http://localhost:9080}"
TAPC="${TAP_URL:-http://localhost:9091}"
BIG_KB="${BIG_KB:-600}"
RUN="p2-$(date +%Y%m%d%H%M%S)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }

IFS=',' read -r -a DEVS <<< "$DEV_KEYS"
DEV1_NAME="${DEVS[0]%%:*}"; DEV1_KEY="${DEVS[0]#*:}"
DEV2_NAME="${DEVS[1]%%:*}"; DEV2_KEY="${DEVS[1]#*:}"

# Newest tap record of a session, waiting up to ~15s for it to be written.
rec_of() {
  local f
  for _ in $(seq 1 30); do
    f=$(ls -1 "logs/tap/$1"/*.json 2>/dev/null | tail -1)
    [[ -n "$f" ]] && { echo "$f"; return 0; }
    sleep 0.5
  done
  return 1
}
# check_rec <label> <record file> <python expression over r>; prints the record summary.
check_rec() {
  if python3 - "$2" "$3" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
u = r.get("usage") or {}
print(f"    {sys.argv[1]}: mode={r['mode']} status={r['status']} consumer={r['consumer']} "
      f"session_source={r['session_source']} body={r['request_body_bytes']}B "
      f"ttft={r.get('ttft_s')} message_start={r.get('message_start_s')} total={r.get('total_s')} "
      f"usage={json.dumps(u)} error={r.get('error')}")
sys.exit(0 if eval("(" + sys.argv[2] + ")") else 1)
EOF
  then ok "$1"; else fail "$1"; fi
}

echo "== waiting for tap at $TAPC"
for _ in $(seq 1 30); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "$TAPC/")" != "000" ]] && break
  sleep 1
done

stream_body() { printf '{"model":"%s","max_tokens":%s,"stream":true,"messages":[{"role":"user","content":"%s"}]}' "$MAAS_MODEL_MAIN" "$1" "$2"; }

echo "== 1. mode B stream (client -> APISIX -> tap -> MaaS)"
if curl -sS -N --max-time 600 -X POST "$BASE/v1/messages?beta=true" \
     -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
     -H "x-api-key: $DEV1_KEY" -H "X-Claude-Code-Session-Id: $RUN-b" \
     --data "$(stream_body 512 'Count from 1 to 40, one number per line, nothing else.')" \
     -w '\n__HTTP_STATUS__:%{http_code}\n' | python3 -u scripts/sse_timing.py > "$TMP/1.out"; then
  ok "1a SSE incremental through APISIX + tap"
else
  fail "1a SSE through APISIX + tap"; cat "$TMP/1.out"
fi
f=$(rec_of "$RUN-b") || f=/dev/null
check_rec "1b tap record: 200, usage, timings, consumer, request id, lab headers stripped" "$f" \
  "r['mode']=='B' and r['status']==200 and 'input_tokens' in u and 'cache_read_input_tokens' in u
   and r['ttft_s'] is not None and r['message_start_s'] is not None and r['consumer']=='$DEV1_NAME'
   and r['lab_request_id'] and r['session_source']=='header' and r['path'].endswith('?beta=true')
   and not any(k.lower().startswith('x-lab-') for k, _ in r['forwarded_headers'])
   and not any(k.lower()=='x-api-key' for k, _ in r['request_headers'])"

echo "== 2. large body x2 through APISIX (${BIG_KB} KiB)"
python3 - "$TMP/big.json" "$BIG_KB" "$MAAS_MODEL_MAIN" "$RUN" <<'EOF'
import json, sys
path, kb, model, run = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
words = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron "
         "pi rho sigma tau upsilon phi chi psi omega").split()
lines, size, i = [f"Run id {run}. Reference document follows."], 0, 0
while size < kb * 1024:
    line = f"{i:06d} " + " ".join(words[(i * 7 + j) % len(words)] for j in range(12))
    lines.append(line); size += len(line) + 1; i += 1
text = "\n".join(lines) + "\n\nReply with the single word: done"
json.dump({"model": model, "max_tokens": 16,
           "messages": [{"role": "user", "content": text}]}, open(path, "w"))
EOF
echo "    body: $(wc -c < "$TMP/big.json") bytes"
for n in 1 2; do
  code=$(curl -sS -o "$TMP/big$n.out" -w '%{http_code}' --max-time 600 -X POST "$BASE/v1/messages" \
    -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
    -H "x-api-key: $DEV1_KEY" -H "X-Claude-Code-Session-Id: $RUN-big" --data-binary "@$TMP/big.json")
  echo "    send $n: http $code $(head -c 300 "$TMP/big$n.out" | tr '\n' ' ' | grep -o '"usage":{[^}]*}' || head -c 200 "$TMP/big$n.out")"
done
sleep 1
f=$(ls -1 logs/tap/"$RUN-big"/*.json 2>/dev/null | tail -1); f=${f:-/dev/null}
check_rec "2a tap received the full large body (send 2)" "$f" \
  "r['request_body_bytes'] == $(wc -c < "$TMP/big.json")"
if [[ "$code" == 200 ]]; then ok "2b MaaS accepted the large body"; else fail "2b MaaS large body http $code (see above)"; fi

echo "== 3. mode C (client -> tap -> MaaS)"
if curl -sS -N --max-time 600 -X POST "$TAPC/v1/messages?beta=true" \
     -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
     -H "Authorization: Bearer $DEV2_KEY" -H "X-Claude-Code-Session-Id: $RUN-c" \
     --data "$(stream_body 512 'Count from 1 to 40, one number per line, nothing else.')" \
     -w '\n__HTTP_STATUS__:%{http_code}\n' | python3 -u scripts/sse_timing.py > "$TMP/3.out"; then
  ok "3a SSE incremental through tap (mode C)"
else
  fail "3a SSE mode C"; cat "$TMP/3.out"
fi
f=$(rec_of "$RUN-c") || f=/dev/null
check_rec "3b mode C record: dev key mapped to consumer, MaaS key forwarded (redacted)" "$f" \
  "r['mode']=='C' and r['status']==200 and r['consumer']=='$DEV2_NAME' and 'input_tokens' in u
   and ['$MAAS_AUTH_HEADER', '<redacted>'] in r['forwarded_headers']"
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$TAPC/v1/messages" -H "x-api-key: wrong" --data '{}')
[[ "$code" == 401 ]] && ok "3c mode C wrong key -> 401" || fail "3c mode C wrong key -> $code"

echo "== 4. client aborts mid-stream"
curl -sS -N --max-time 4 -X POST "$BASE/v1/messages" \
  -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
  -H "x-api-key: $DEV1_KEY" -H "X-Claude-Code-Session-Id: $RUN-abort" \
  --data "$(stream_body 4000 'Count from 1 to 1500, one number per line, nothing else.')" > /dev/null 2>&1
f=$(rec_of "$RUN-abort") || f=/dev/null
check_rec "4 aborted stream recorded as client_disconnected / 499" "$f" \
  "(r.get('error') or {}).get('status') == 499"

echo "== 5. APISIX body vs tap body"
if python3 scripts/diff_bodies.py | sed 's/^/    /'; [[ ${PIPESTATUS[0]} == 0 ]]; then
  ok "5 every body APISIX received reached the tap byte-for-byte; headers unchanged"
else
  fail "5 diff found differences or nothing comparable (reports/apisix-vs-tap.md)"
fi

echo "== 6. secrets not in tap logs"
leaks=0
for secret in "$MAAS_API_KEY" "$DEV1_KEY" "$DEV2_KEY"; do
  grep -rqF -- "$secret" logs/tap/ && leaks=1
done
[[ $leaks == 0 ]] && ok "6 no MaaS/dev key in logs/tap" || fail "6 a key was found in logs/tap"

echo
echo "run id: $RUN   records: logs/tap/$RUN-*"
echo "passed=$PASS failed=$FAIL"
[[ $FAIL == 0 ]]
