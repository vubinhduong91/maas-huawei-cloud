#!/usr/bin/env bash
# Key-order lab Phase 5: replay test. Three captured Claude Code bodies (small, medium,
# large) are each sent 30 times (concurrency 1 and 8) through every path; the upstream is
# the replay echo server (scenarios/replay/replay.py), so no tokens are spent and only the
# gateway can make the bodies differ. Gateway profiles are re-run with concurrency 8 after
# an nginx reload and after an APISIX container restart.
#   direct  client -> tap-pre -> echo              (control, no gateway)
#   P0      client -> tap-pre -> APISIX -> tap-post -> echo   (no body plugin), 4 workers
#   P2-w1 / P2  pre-function core.json decode + encode, 1 / 4 workers
#   P1      ai-proxy (APISIX 3.14.1), 4 workers
#   P2S     pre-function decode + core.json.stably_encode (dkjson, sorted keys), 4 workers
# While it runs, GLM requests of the daily lab get the echo's "OK"; the lab is restored at the end.
# Usage: scenarios/keyorder_phase5.sh ["direct P0 P2-w1 P2 P1"]
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LAB"
CONFIGS="${1:-direct P0 P2-w1 P2 P1}"
N="${REPLAY_N:-30}"
OUT="$LAB/logs/replay"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
WINDOWS="$OUT/windows-$RUN_ID.jsonl"
mkdir -p "$OUT/bodies"
# docker compose is called directly here: same uid/gid as the Makefile sets.
export LAB_UID="$(id -u)" LAB_GID="$(id -g)"
DEV_KEY="$(sed -n 's/^DEV_KEYS=//p' .env | tr -d "'\"" | tr ',' '\n' | sed -n 's/^dev_alice://p')"
export REPLAY_API_KEY="$DEV_KEY"

# Bodies: Claude Code requests captured at tap-pre (Phase 2/4): smallest above 40 KB,
# median and largest by size.
python3 - "$LAB/logs/raw/pre" "$OUT/bodies" <<'EOF'
import json, shutil, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
cands = []
for p in src.glob("*.json"):
    try:
        m = json.loads(p.read_text())
    except ValueError:
        continue
    s = str(m.get("session", ""))
    if m.get("status") != 200 or "count_tokens" in (m.get("path") or "") or s.count("-") != 4 or s.startswith("p"):
        continue
    if m.get("transform") or m.get("request_body_bytes", 0) < 40000:
        continue
    cands.append((m["request_body_bytes"], p.with_suffix(".body")))
cands.sort()
for name, (size, body) in zip(("small", "medium", "large"), (cands[0], cands[len(cands) // 2], cands[-1])):
    shutil.copy(body, dst / f"{name}.json")
    print(f"body {name}: {size} bytes ({body.name})")
EOF

send() {   # tag body concurrency
  python3 scenarios/replay/replay.py send --base-url http://127.0.0.1:9001/v1/messages?beta=true \
    --body-file "$OUT/bodies/$2.json" -n "$N" --concurrency "$3" --tag "$1" --windows "$WINDOWS" \
    | python3 -c 'import json,sys; o=json.loads(sys.stdin.read()); print("  ", o["tag"], o["status"], o["seconds"], "s")
sys.exit(0 if set(o["status"]) == {"200"} else 1)' || { echo "STOP: $1 did not get 30x200"; exit 5; }
}

gateway() {   # config
  unset LAB_GATEWAY_PROFILE APISIX_WORKER_PROCESSES APISIX_IMAGE_TAG TAP_POST_RAW_ALL TAP_PRE_UPSTREAM
  export TAP_POST_UPSTREAM_ORIGIN=http://replay-echo:9099 TAP_PRE_UPSTREAM_ORIGIN=http://replay-echo:9099
  case "$1" in
    direct) export TAP_PRE_UPSTREAM=maas ;;
    P0)     export LAB_GATEWAY_PROFILE=P0 APISIX_WORKER_PROCESSES=4 ;;
    P2-w1)  export LAB_GATEWAY_PROFILE=P2 APISIX_WORKER_PROCESSES=1 ;;
    P2)     export LAB_GATEWAY_PROFILE=P2 APISIX_WORKER_PROCESSES=4 ;;
    P2S)    export LAB_GATEWAY_PROFILE=P2S APISIX_WORKER_PROCESSES=4 ;;
    P1)     export LAB_GATEWAY_PROFILE=P1 APISIX_WORKER_PROCESSES=4 APISIX_IMAGE_TAG=3.14.1-debian TAP_POST_RAW_ALL=1 ;;
  esac
  make -s up MODE=B 2>&1 | grep -E "render:|rror" || true
  make -s keyorder-up >/dev/null 2>&1
  sleep 4
}

restore() {
  echo "== restore default lab (P0, APISIX 3.11, workers auto, tap -> MaaS)"
  unset LAB_GATEWAY_PROFILE APISIX_WORKER_PROCESSES APISIX_IMAGE_TAG TAP_POST_RAW_ALL TAP_PRE_UPSTREAM \
        TAP_POST_UPSTREAM_ORIGIN TAP_PRE_UPSTREAM_ORIGIN
  make -s up MODE=B >/dev/null 2>&1
  make -s keyorder-up >/dev/null 2>&1
  docker compose --profile keyorder stop replay-echo >/dev/null 2>&1
}
trap restore EXIT

[[ -f "$OUT/echo.jsonl" ]] && mv "$OUT/echo.jsonl" "$OUT/echo-before-$RUN_ID.jsonl"
docker compose --profile keyorder up -d --force-recreate replay-echo >/dev/null 2>&1
sleep 2
for cfg in $CONFIGS; do
  gateway "$cfg"
  echo "== $cfg ($(docker exec maas-cache-lab-apisix-1 sh -c 'grep -m1 ^worker_processes conf/nginx.conf' 2>/dev/null))"
  for body in small medium large; do
    for c in 1 8; do send "$cfg/$body/c$c" "$body" "$c"; done
  done
  [[ "$cfg" == direct ]] && continue
  docker exec maas-cache-lab-apisix-1 /usr/local/openresty/bin/openresty -p /usr/local/apisix -s reload
  sleep 3
  for body in small medium large; do send "$cfg/$body/c8-reload" "$body" 8; done
  docker restart maas-cache-lab-apisix-1 >/dev/null
  sleep 8
  for body in small medium large; do send "$cfg/$body/c8-restart" "$body" 8; done
done

REPORT="$LAB/reports/keyorder-phase5-$RUN_ID.md"
{
  echo "# Key-order lab Phase 5: replay test (run $RUN_ID)"
  echo
  echo "Each body sent $N times per row through tap-pre to the replay echo server; see scenarios/replay/README.md for the columns."
  for body in small medium large; do
    echo; echo "## $body ($(wc -c < "$OUT/bodies/$body.json") bytes)"; echo
    python3 scenarios/replay/replay.py report --log "$OUT/echo.jsonl" --windows "$WINDOWS" \
      --reference "$OUT/bodies/$body.json" | grep -E "^\| tag |^\|---|/$body/"
  done
} > "$REPORT"
cat "$REPORT"
echo "report $REPORT"
