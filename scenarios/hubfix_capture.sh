#!/usr/bin/env bash
# Hub-fix lab Phase 0, step 2: what Claude Code sends at each thinking/effort level.
# One `claude -p` per case, Claude Code -> tap-pre (TAP_PRE_UPSTREAM=maas: straight to MaaS, so a
# Sonnet model name reaches MaaS instead of the lab's Claude passthrough) -> MaaS.
# Each case gets a fixed --session-id; logs/hubfix/capture.jsonl maps case -> session, and
# scenarios/hubfix_extract.py reads the matching logs/raw/pre bodies.
#   G-*  model glm-5.3 (lab default env)     S-*  LAB_NATIVE_MODELS=1 --model sonnet
# Restores tap-pre to its default (upstream APISIX) at the end.
# Usage: scenarios/hubfix_capture.sh ["G-default G-low ..."]
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LAB"
DEV="${HUBFIX_DEV:-dev_bob}"
PROMPT="Reply with exactly the word OK and nothing else. Do not use any tools."
LEVELS="low medium high xhigh max"
DEFAULT_CASES="G-default $(printf 'G-%s ' $LEVELS) G-env-max G-always-effort G-max-thinking
               S-default $(printf 'S-%s ' $LEVELS) S-max-thinking"
CASES="${1:-$DEFAULT_CASES}"
OUT="$LAB/logs/hubfix"; mkdir -p "$OUT"

restore() {
  echo "== restore tap-pre (upstream APISIX)"
  unset TAP_PRE_UPSTREAM
  make -s keyorder-up >/dev/null 2>&1
}
trap restore EXIT
export TAP_PRE_UPSTREAM=maas
make -s keyorder-up >/dev/null 2>&1
sleep 3

for case in $CASES; do
  args=() envs=()
  case "$case" in
    G-default)       ;;
    G-env-max)       envs=(CLAUDE_CODE_EFFORT_LEVEL=max) ;;
    G-always-effort) envs=(CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1) ;;
    G-max-thinking)  envs=(MAX_THINKING_TOKENS=16000) ;;
    G-*)             args=(--effort "${case#G-}") ;;
    S-default)       envs=(LAB_NATIVE_MODELS=1); args=(--model sonnet) ;;
    S-max-thinking)  envs=(LAB_NATIVE_MODELS=1 MAX_THINKING_TOKENS=16000); args=(--model sonnet) ;;
    S-*)             envs=(LAB_NATIVE_MODELS=1); args=(--model sonnet --effort "${case#S-}") ;;
    *) echo "unknown case $case"; continue ;;
  esac
  sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  out=$(env "${envs[@]}" timeout 180 bash scripts/run-claude.sh "$DEV" P -p "$PROMPT" --session-id "$sid" \
          --output-format json "${args[@]}" 2>&1); rc=$?
  result=$(printf '%s' "$out" | python3 -c 'import json,sys
t=sys.stdin.read()
for line in t.splitlines():
    try:
        d=json.loads(line[line.index("{"):])
    except ValueError:
        continue
    if isinstance(d, dict) and d.get("type") == "result":
        print(json.dumps({k:d.get(k) for k in ("is_error","api_error_status","result","num_turns")})[:400]); break
else:
    print(json.dumps({"raw": t[-400:]}))')
  python3 - "$OUT/capture.jsonl" "$case" "$sid" "$ts" "$rc" "$result" "${envs[*]:-}" "${args[*]:-}" <<'EOF'
import json, sys
path, case, sid, ts, rc, result, envs, args = sys.argv[1:]
with open(path, "a") as f:
    f.write(json.dumps({"case": case, "session": sid, "ts": ts, "rc": int(rc), "env": envs,
                        "args": args, "claude": json.loads(result)}) + "\n")
EOF
  echo "$case rc=$rc session=$sid $result"
done
