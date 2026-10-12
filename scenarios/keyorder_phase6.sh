#!/usr/bin/env bash
# Key-order lab Phase 6: verify the fixes. One short Claude Code session (variant 2b,
# scenarios/prompts/keyorder-short.txt) per row, Claude Code -> tap-pre -> APISIX -> tap-post -> MaaS:
#   p6-P2-pass       P2 (cjson re-encode), 4 workers, tap-post passthrough   baseline with drift
#   p6-P2-canon      same, tap-post TAP_MODE=canon (keys sorted just before MaaS)
#   p6-P2S           P2S: re-encode with core.json.stably_encode (dkjson, sorted keys) in APISIX
#   p6-shuffle-first P0, tap-post TAP_MODE=shuffle on tools[0]       positive control
#   p6-shuffle-last  P0, tap-post TAP_MODE=shuffle on the last tool  positive control
# Restores the default lab at the end, then writes reports/keyorder-phase6.md.
# Usage: scenarios/keyorder_phase6.sh ["p6-P2-pass p6-P2-canon ..."]
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LAB"
RUNS="${1:-p6-P2-pass p6-P2-canon p6-P2S p6-shuffle-first p6-shuffle-last}"
export KEYORDER_PROMPTS="${KEYORDER_PROMPTS:-$LAB/scenarios/prompts/keyorder-short.txt}"
VARS="LAB_GATEWAY_PROFILE APISIX_WORKER_PROCESSES APISIX_IMAGE_TAG TAP_POST_RAW_ALL TAP_PRE_UPSTREAM TAP_PRE_MODE
      TAP_POST_MODE TAP_POST_SHUFFLE_TOOL_INDEX TAP_POST_SHUFFLE_SEED TAP_POST_UPSTREAM_ORIGIN TAP_PRE_UPSTREAM_ORIGIN"

restore() {
  echo "== restore default lab (P0, APISIX 3.11, workers auto, tap-post passthrough)"
  unset $VARS
  make -s up MODE=B >/dev/null 2>&1
  make -s keyorder-up >/dev/null 2>&1
}
trap restore EXIT

for run in $RUNS; do
  unset $VARS
  case "$run" in
    p6-P2-pass)       export LAB_GATEWAY_PROFILE=P2 APISIX_WORKER_PROCESSES=4 ;;
    p6-P2-canon)      export LAB_GATEWAY_PROFILE=P2 APISIX_WORKER_PROCESSES=4 TAP_POST_MODE=canon ;;
    p6-P2S)           export LAB_GATEWAY_PROFILE=P2S APISIX_WORKER_PROCESSES=4 ;;
    p6-shuffle-first) export LAB_GATEWAY_PROFILE=P0 TAP_POST_MODE=shuffle TAP_POST_SHUFFLE_TOOL_INDEX=0 ;;
    p6-shuffle-last)  export LAB_GATEWAY_PROFILE=P0 TAP_POST_MODE=shuffle TAP_POST_SHUFFLE_TOOL_INDEX=-1 ;;
    *) echo "unknown run $run"; continue ;;
  esac
  make -s up MODE=B 2>&1 | grep -E "render:|rror" || true
  make -s keyorder-up >/dev/null 2>&1
  sleep 4
  started=$(docker logs maas-cache-lab-tap-1 2>&1 | grep -m1 "^tap post")
  echo "== $run | $started"
  printf '{"label":"%s","gateway":"%s","workers":"%s","tap_post_mode":"%s","shuffle_index":"%s","ts":"%s"}\n' \
    "$run" "${LAB_GATEWAY_PROFILE:-P0}" "${APISIX_WORKER_PROCESSES:-auto}" "${TAP_POST_MODE:-passthrough}" \
    "${TAP_POST_SHUFFLE_TOOL_INDEX:-}" "$(date -u +%FT%TZ)" >> logs/keyorder/phase6-configs.jsonl
  bash scenarios/session_script.sh 2b "$run" || echo "   session_script exit $?"
done
python3 scenarios/keyorder_phase6.py
