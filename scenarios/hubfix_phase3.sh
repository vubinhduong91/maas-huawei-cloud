#!/usr/bin/env bash
# Hub-fix lab Phase 3 on the server: replay and overhead, upstream = replay echo (no tokens spent
# except the overhead calibration: a few calls to MaaS to measure the token size of the bodies).
#   replay    3 Sonnet bodies (from the p1-* sessions) x effort medium / xhigh, each sent $REPLAY_N
#             (30) times at concurrency 1 and 8 through: direct (no gateway), P0-w1/P0, P3-w1/P3,
#             P4-w1/P4, P5-w1/P5. direct and P0 get the already-mapped body (a client that maps),
#             the others the Sonnet body. Reference for the report = the body P4 must send.
#   overhead  ~50K and ~160K token bodies, $OVERHEAD_N (100) times at concurrency 1 through the same
#             configs; per request: x-lab-hubfix-us (CPU time of the gateway's body handling, at
#             tap-post) and tap-pre total_s (end to end through the gateway to the echo).
# While it runs, GLM requests of the daily lab get the echo's "OK"; the lab is restored (P0,
# workers auto, tap -> MaaS) at the end. Results: reports/hubfix-phase3-replay-<run>.md and
# reports/hubfix-phase3-overhead-<run>.md (scenarios/hubfix_overhead.py).
# Usage: scenarios/hubfix_phase3.sh [replay] [overhead]
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LAB"
STAGES="${*:-replay overhead}"
CONFIGS="${HUBFIX_CONFIGS:-direct P0-w1 P0 P3-w1 P3 P4-w1 P4 P5-w1 P5}"
N="${REPLAY_N:-30}"
ON="${OVERHEAD_N:-100}"
OUT="$LAB/logs/replay"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
WINDOWS="$OUT/windows-hubfix3-$RUN_ID.jsonl"
BODIES="$OUT/bodies-hubfix"
OBODIES="$OUT/bodies-hubfix-overhead"
mkdir -p "$BODIES" "$OBODIES" "$LAB/reports"
export LAB_UID="$(id -u)" LAB_GID="$(id -g)"
export REPLAY_API_KEY="$(sed -n 's/^DEV_KEYS=//p' .env | tr -d "'\"" | tr ',' '\n' | sed -n 's/^dev_alice://p')"

# Bodies. small/medium/large.json were picked by scenarios/hubfix_replay.sh (Phase 1).
[[ -f "$BODIES/large.json" ]] || { echo "missing $BODIES/large.json: run scenarios/hubfix_replay.sh once"; exit 2; }
python3 scenarios/hubfix_bodies.py replay "$BODIES" || exit 2
if [[ "$STAGES" == *overhead* && ! -f "$OBODIES/160k.json" ]]; then
  python3 scenarios/hubfix_bodies.py overhead "$OBODIES" --base "$BODIES/large-medium.json" || exit 2
fi

send() {   # tag body-file n concurrency
  python3 scenarios/replay/replay.py send --base-url http://127.0.0.1:9001/v1/messages?beta=true \
    --body-file "$2" -n "$3" --concurrency "$4" --tag "$1" --windows "$WINDOWS" \
    | python3 -c 'import json,sys; o=json.loads(sys.stdin.read()); print("  ", o["tag"], o["status"], o["seconds"], "s")
sys.exit(0 if set(o["status"]) == {"200"} else 1)' || { echo "STOP: $1 did not get all 200"; exit 5; }
}

gateway() {   # config
  unset LAB_GATEWAY_PROFILE APISIX_WORKER_PROCESSES TAP_PRE_UPSTREAM
  export TAP_POST_UPSTREAM_ORIGIN=http://replay-echo:9099 TAP_PRE_UPSTREAM_ORIGIN=http://replay-echo:9099
  case "$1" in
    direct) export TAP_PRE_UPSTREAM=maas ;;
    *-w1)   export LAB_GATEWAY_PROFILE="${1%-w1}" APISIX_WORKER_PROCESSES=1 ;;
    *)      export LAB_GATEWAY_PROFILE="$1" APISIX_WORKER_PROCESSES=4 ;;
  esac
  if [[ "$1" != direct ]]; then   # never bring APISIX down with a config that does not render
    make -s render-check >/dev/null 2>&1 || { echo "STOP: render-check failed for $1"; exit 6; }
  fi
  make -s up MODE=B 2>&1 | grep -E "rror" || true
  make -s keyorder-up >/dev/null 2>&1
  sleep 4
}

restore() {
  echo "== restore default lab (P0, APISIX 3.11, workers auto, tap -> MaaS)"
  unset LAB_GATEWAY_PROFILE APISIX_WORKER_PROCESSES TAP_PRE_UPSTREAM TAP_POST_UPSTREAM_ORIGIN TAP_PRE_UPSTREAM_ORIGIN
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
  sfx=""; [[ "$cfg" == direct || "$cfg" == P0* ]] && sfx=".expected"
  if [[ "$STAGES" == *replay* ]]; then
    for body in small medium large; do
      for level in medium xhigh; do
        for c in 1 8; do send "$cfg/$body-$level/c$c" "$BODIES/$body-$level$sfx.json" "$N" "$c"; done
      done
    done
  fi
  if [[ "$STAGES" == *overhead* ]]; then
    for size in 50k 160k; do send "overhead/$cfg/$size" "$OBODIES/$size$sfx.json" "$ON" 1; done
  fi
done
cp "$OUT/echo.jsonl" "$OUT/echo-hubfix3-$RUN_ID.jsonl"

if [[ "$STAGES" == *replay* ]]; then
  REPORT="$LAB/reports/hubfix-phase3-replay-$RUN_ID.md"
  {
    echo "# Hub-fix Phase 3: replay (run $RUN_ID)"
    echo
    echo "Each body sent $N times per row through tap-pre to the replay echo server. direct and P0 are sent"
    echo "the already-mapped body, P3/P4/P5 the Sonnet body. Reference (\"same bytes as reference\") ="
    echo "the body P4 must send: the client bytes with only model and output_config.effort replaced."
    for body in small medium large; do
      for level in medium xhigh; do
        echo; echo "## $body, effort $level ($(wc -c < "$BODIES/$body-$level.json") bytes)"; echo
        python3 scenarios/replay/replay.py report --log "$OUT/echo.jsonl" --windows "$WINDOWS" \
          --reference "$BODIES/$body-$level.expected.json" | grep -E "^\| tag |^\|---|/$body-$level/"
      done
    done
  } > "$REPORT"
  cat "$REPORT"
  echo "report $REPORT"
fi
if [[ "$STAGES" == *overhead* ]]; then
  python3 scenarios/hubfix_overhead.py --windows "$WINDOWS" --out "reports/hubfix-phase3-overhead-$RUN_ID.md"
fi
