#!/usr/bin/env bash
# Hub-fix lab: replay test of a gateway profile on Sonnet bodies, as in key-order Phase 5. Three
# Claude Code bodies with model claude-sonnet-* (captured at tap-pre in hub-fix sessions whose
# label starts with $HUBFIX_BODY_PREFIX, default p1-) are each sent $REPLAY_N (30) times,
# concurrency 1 and 8, through every config; the upstream is the replay echo server, so no
# tokens are spent and only the gateway can make the bodies differ.
#   direct   client -> tap-pre -> echo   (control, no gateway)
#   P3-w1/P3 decode -> map Sonnet/effort -> core.json.encode, 1 / 4 workers
#   P4-w1/P4, P5-w1/P5   (Phase 2 profiles, once they exist in apisix/render.py)
# A Sonnet body cannot go through P0 here: P0 does not map the model, so the Claude passthrough
# would send it to Anthropic. While it runs, GLM requests of the daily lab get the echo's "OK";
# the lab is restored (P0, workers auto, tap -> MaaS) at the end.
# Usage: scenarios/hubfix_replay.sh ["direct P3-w1 P3"]
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LAB"
CONFIGS="${1:-direct P3-w1 P3}"
N="${REPLAY_N:-30}"
PREFIX="${HUBFIX_BODY_PREFIX:-p1-}"
OUT="$LAB/logs/replay"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
WINDOWS="$OUT/windows-hubfix-$RUN_ID.jsonl"
BODIES="$OUT/bodies-hubfix"
mkdir -p "$BODIES"
export LAB_UID="$(id -u)" LAB_GID="$(id -g)"
DEV_KEY="$(sed -n 's/^DEV_KEYS=//p' .env | tr -d "'\"" | tr ',' '\n' | sed -n 's/^dev_alice://p')"
export REPLAY_API_KEY="$DEV_KEY"

python3 - "$LAB/logs/raw/pre" "$LAB/logs/hubfix/sessions.jsonl" "$PREFIX" "$BODIES" <<'EOF'
import json, shutil, sys
from pathlib import Path
src, sessions_file, prefix, dst = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
sessions = set()
for line in sessions_file.read_text(encoding="utf-8-sig").splitlines():
    r = json.loads(line)
    if r.get("label", "").startswith(prefix) and r.get("client") == "sonnet":
        sessions.add(r["session"])
cands = []
for p in src.glob("*.json"):
    try:
        m = json.loads(p.read_text())
    except ValueError:
        continue
    if m.get("session") not in sessions or m.get("status") != 200 or "count_tokens" in (m.get("path") or ""):
        continue
    if not str(m.get("request_model") or "").startswith("claude-sonnet-") or m.get("request_body_bytes", 0) < 40000:
        continue
    cands.append((m["request_body_bytes"], p.with_suffix(".body")))
if len(cands) < 3:
    sys.exit(f"need 3 Sonnet bodies >= 40 KB from sessions {prefix}*, found {len(cands)}")
cands.sort()
for name, (size, body) in zip(("small", "medium", "large"), (cands[0], cands[len(cands) // 2], cands[-1])):
    shutil.copy(body, dst / f"{name}.json")
    print(f"body {name}: {size} bytes ({body.name})")
EOF
[[ $? == 0 ]] || exit 2

send() {   # tag body concurrency
  python3 scenarios/replay/replay.py send --base-url http://127.0.0.1:9001/v1/messages?beta=true \
    --body-file "$BODIES/$2.json" -n "$N" --concurrency "$3" --tag "$1" --windows "$WINDOWS" \
    | python3 -c 'import json,sys; o=json.loads(sys.stdin.read()); print("  ", o["tag"], o["status"], o["seconds"], "s")
sys.exit(0 if set(o["status"]) == {"200"} else 1)' || { echo "STOP: $1 did not get ${N}x200"; exit 5; }
}

gateway() {   # config
  unset LAB_GATEWAY_PROFILE APISIX_WORKER_PROCESSES TAP_PRE_UPSTREAM
  export TAP_POST_UPSTREAM_ORIGIN=http://replay-echo:9099 TAP_PRE_UPSTREAM_ORIGIN=http://replay-echo:9099
  case "$1" in
    direct) export TAP_PRE_UPSTREAM=maas ;;
    *-w1)   export LAB_GATEWAY_PROFILE="${1%-w1}" APISIX_WORKER_PROCESSES=1 ;;
    *)      export LAB_GATEWAY_PROFILE="$1" APISIX_WORKER_PROCESSES=4 ;;
  esac
  make -s up MODE=B 2>&1 | grep -E "render:|rror" || true
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
  for body in small medium large; do
    for c in 1 8; do send "$cfg/$body/c$c" "$body" "$c"; done
  done
done
cp "$OUT/echo.jsonl" "$OUT/echo-hubfix-$RUN_ID.jsonl"

REPORT="$LAB/reports/hubfix-replay-$RUN_ID.md"
{
  echo "# Hub-fix lab: replay test (run $RUN_ID, configs: $CONFIGS)"
  echo
  echo "Each Sonnet body sent $N times per row through tap-pre to the replay echo server; see scenarios/replay/README.md for the columns."
  for body in small medium large; do
    echo; echo "## $body ($(wc -c < "$BODIES/$body.json") bytes)"; echo
    python3 scenarios/replay/replay.py report --log "$OUT/echo.jsonl" --windows "$WINDOWS" \
      --reference "$BODIES/$body.json" | grep -E "^\| tag |^\|---|/$body/"
  done
} > "$REPORT"
cat "$REPORT"
echo "report $REPORT"
