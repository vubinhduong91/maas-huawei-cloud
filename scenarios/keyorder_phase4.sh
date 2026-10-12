#!/usr/bin/env bash
# Key-order lab Phase 4: Claude Code -> tap-pre -> APISIX -> tap-post -> MaaS, one short
# session (variant 2b, scenarios/prompts/keyorder-short.txt) per gateway profile and
# worker count. Ends by restoring the default lab (P0, APISIX 3.11, workers auto) and
# writing reports/keyorder-phase4.md (scenarios/keyorder_phase4.py).
#
# Usage: scenarios/keyorder_phase4.sh ["P0 P2 P1"] ["1 4"]
#   P0  key-auth + header rewrite only (the lab as it is)
#   P1  ai-proxy (provider openai-compatible) on APISIX 3.14.1: decodes, overrides model,
#       re-encodes; drops client headers, so tap-post keeps every body (TAP_POST_RAW_ALL=1)
#   P2  serverless-pre-function: core.json.decode + core.json.encode + set body (APISIX 3.11)
set -uo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LAB"
PROFILES="${1:-P0 P2 P1}"
WORKERS="${2:-1 4}"
export KEYORDER_PROMPTS="${KEYORDER_PROMPTS:-$LAB/scenarios/prompts/keyorder-short.txt}"
unset TAP_PRE_UPSTREAM TAP_PRE_MODE

restore() {
  echo "== restore default lab (P0, APISIX 3.11, workers auto)"
  unset LAB_GATEWAY_PROFILE APISIX_WORKER_PROCESSES APISIX_IMAGE_TAG TAP_POST_RAW_ALL
  make -s up MODE=B >/dev/null 2>&1
  make -s keyorder-up >/dev/null 2>&1
}
trap restore EXIT

for P in $PROFILES; do
  for W in $WORKERS; do
    export LAB_GATEWAY_PROFILE="$P" APISIX_WORKER_PROCESSES="$W"
    if [[ "$P" == P1 ]]; then
      export APISIX_IMAGE_TAG=3.14.1-debian TAP_POST_RAW_ALL=1
    else
      unset APISIX_IMAGE_TAG TAP_POST_RAW_ALL
    fi
    echo "== $P workers=$W image=${APISIX_IMAGE_TAG:-3.11.0-debian}"
    make -s up MODE=B 2>&1 | grep -E "render:|error" || true
    make -s keyorder-up >/dev/null 2>&1
    sleep 3
    workers=$(docker exec maas-cache-lab-apisix-1 sh -c 'grep -m1 "^worker_processes" /usr/local/apisix/conf/nginx.conf')
    nworkers=$(docker top maas-cache-lab-apisix-1 | grep -c "nginx: worker process")
    version=$(docker exec maas-cache-lab-apisix-1 sh -c 'cd /usr/local/apisix && /usr/bin/apisix version 2>/dev/null' | tail -1)
    echo "   apisix $version, nginx.conf '$workers', running workers: $nworkers"
    printf '{"label":"p4-%s-w%s","profile":"%s","workers_conf":"%s","workers_running":%s,"apisix_version":"%s","ts":"%s"}\n' \
      "$P" "$W" "$P" "$W" "$nworkers" "$version" "$(date -u +%FT%TZ)" >> logs/keyorder/phase4-gateways.jsonl
    bash scenarios/session_script.sh 2b "p4-$P-w$W" || echo "   session_script exit $?"
  done
done
python3 scenarios/keyorder_phase4.py
