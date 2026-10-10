#!/usr/bin/env bash
# Probe MaaS directly (no APISIX) to verify facts the docs leave ambiguous:
#   - which auth header works (x-api-key vs Authorization: Bearer)
#   - whether /v1/messages/count_tokens exists
#   - whether ?beta=true (sent by Claude Code) is accepted
#   - whether both model names are accepted
# Prints status codes and response snippets only; the key is never echoed.
set -uo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "missing .env (copy .env.example)"; exit 2; }
set -a; . ./.env; set +a
[[ -n "${MAAS_API_KEY:-}" ]] || { echo "MAAS_API_KEY empty"; exit 2; }

URL="${MAAS_BASE_URL%/}/v1"
OUT="$(mktemp)"; trap 'rm -f "$OUT"' EXIT

body() { printf '{"model":"%s","max_tokens":32,"messages":[{"role":"user","content":"Reply with: pong"}]}' "$1"; }

probe() { # probe <label> <path> <body> [header args...]
  local label="$1" path="$2" data="$3"; shift 3
  local code
  code=$(curl -sS -o "$OUT" -w '%{http_code}' --max-time 120 -X POST "$URL$path" \
    -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' "$@" --data "$data")
  printf '%-42s http %s  %s\n' "$label" "$code" "$(head -c 300 "$OUT" | tr '\n' ' ')"
}

echo "endpoint: $URL"
probe "auth x-api-key"              /messages "$(body "$MAAS_MODEL_MAIN")" -H "x-api-key: $MAAS_API_KEY"
probe "auth Authorization: Bearer"  /messages "$(body "$MAAS_MODEL_MAIN")" -H "Authorization: Bearer $MAAS_API_KEY"
probe "no auth (expect 401/403)"    /messages "$(body "$MAAS_MODEL_MAIN")"

AUTH=(-H "${MAAS_AUTH_HEADER}: ${MAAS_AUTH_SCHEME:+$MAAS_AUTH_SCHEME }$MAAS_API_KEY")
probe "query ?beta=true"            "/messages?beta=true" "$(body "$MAAS_MODEL_MAIN")" "${AUTH[@]}"
probe "model MAAS_MODEL_FAST"       /messages "$(body "$MAAS_MODEL_FAST")" "${AUTH[@]}"
probe "model glm-5.3"               /messages "$(body glm-5.3)" "${AUTH[@]}"
probe "count_tokens"                /messages/count_tokens \
  "$(printf '{"model":"%s","messages":[{"role":"user","content":"hello"}]}' "$MAAS_MODEL_MAIN")" "${AUTH[@]}"
