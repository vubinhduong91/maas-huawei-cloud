#!/usr/bin/env bash
# Run Claude Code against the lab.
# Usage: scripts/run-claude.sh <dev> <A|B|C> [--background] [claude args...]
#   dev           a consumer name from DEV_KEYS (e.g. dev_alice)
#   A|B           ANTHROPIC_BASE_URL = APISIX (:9080); the lab must be up in that mode
#   C             ANTHROPIC_BASE_URL = tap (:9091), bypassing APISIX
#   --background  do NOT set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC (observe background traffic)
# Examples:
#   scripts/run-claude.sh dev_alice B                      # interactive
#   scripts/run-claude.sh dev_alice B -p "Explain the login flow"
# Each dev gets an isolated Claude Code config dir (.lab-claude/<dev>) so the user's own
# settings, memory and login never leak into lab requests. Runs are appended to logs/runs.jsonl.
set -euo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"   # native Claude Code installer location
[[ $# -ge 2 ]] || { sed -n 2,12p "$0"; exit 2; }
DEV="$1"; MODE="${2^^}"; shift 2
BACKGROUND=0
if [[ "${1:-}" == "--background" ]]; then BACKGROUND=1; shift; fi

set -a; . "$LAB/.env"; set +a
KEY=""
IFS=',' read -r -a DEVS <<< "$DEV_KEYS"
for item in "${DEVS[@]}"; do
  [[ "${item%%:*}" == "$DEV" ]] && KEY="${item#*:}"
done
[[ -n "$KEY" ]] || { echo "unknown dev '$DEV' (known: $(printf '%s ' "${DEVS[@]%%:*}"))" >&2; exit 2; }

case "$MODE" in
  A|B) BASE_URL="${APISIX_URL:-http://localhost:9080}" ;;
  C)   BASE_URL="${TAP_URL:-http://localhost:9091}" ;;
  *)   echo "mode must be A, B or C" >&2; exit 2 ;;
esac
# Tap is up iff its mode C listener answers (works without docker group access).
tap_up=$([[ "$(curl -s -o /dev/null -w "%{http_code}" "${TAP_URL:-http://localhost:9091}/__tap/health")" == 000 ]] && echo 0 || echo 1)
if [[ "$MODE" == A && "$tap_up" != 0 ]]; then
  echo "warning: tap is running, so APISIX is probably in mode B (run: make up MODE=A)" >&2
elif [[ "$MODE" != A && "$tap_up" == 0 ]]; then
  echo "error: mode $MODE needs the tap (run: make up MODE=$MODE)" >&2; exit 2
fi

SAMPLE="$LAB/sample-repo"
[[ -d "$SAMPLE/.git" ]] || { echo "missing sample-repo/ (run: scripts/prepare-sample-repo.sh)" >&2; exit 2; }

export CLAUDE_CONFIG_DIR="$LAB/.lab-claude/$DEV"
mkdir -p "$CLAUDE_CONFIG_DIR"
# Skip first-run onboarding and the folder-trust prompt for the sample repo.
if [[ ! -f "$CLAUDE_CONFIG_DIR/.claude.json" ]]; then
  printf '{"hasCompletedOnboarding":true,"projects":{"%s":{"hasTrustDialogAccepted":true}}}\n' \
    "$SAMPLE" > "$CLAUDE_CONFIG_DIR/.claude.json"
fi

unset ANTHROPIC_API_KEY CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX ANTHROPIC_SMALL_FAST_MODEL
export ANTHROPIC_BASE_URL="$BASE_URL"
export ANTHROPIC_AUTH_TOKEN="$KEY"
export ANTHROPIC_MODEL="$MAAS_MODEL_MAIN"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$MAAS_MODEL_MAIN"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$MAAS_MODEL_MAIN"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MAAS_MODEL_FAST"
if [[ $BACKGROUND == 0 ]]; then
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
else
  unset CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
fi

mkdir -p "$LAB/logs"
python3 - "$LAB/logs/runs.jsonl" "$DEV" "$MODE" "$BACKGROUND" "$(claude --version 2>/dev/null)" "$@" <<'EOF'
import json, sys, datetime
path, dev, mode, bg, version, *args = sys.argv[1:]
with open(path, "a") as f:
    f.write(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "dev": dev, "mode": mode, "background_traffic": bg == "1",
                        "claude_version": version, "args": args}) + "\n")
EOF
echo "claude: dev=$DEV mode=$MODE base=$BASE_URL model=$ANTHROPIC_MODEL fast=$ANTHROPIC_DEFAULT_HAIKU_MODEL" \
     "nonessential_traffic=$([[ $BACKGROUND == 1 ]] && echo on || echo off)" >&2
cd "$SAMPLE"
exec claude "$@"
