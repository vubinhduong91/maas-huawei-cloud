#!/usr/bin/env bash
# Key-order lab (Phase 2, reused in Phase 4): one Claude Code session of fixed prompts on
# a private copy of sample-repo, sent through tap-pre (:9001). Where tap-pre forwards
# (straight to MaaS or to APISIX) is set when it is started:
#   TAP_PRE_UPSTREAM=maas make keyorder-up      # Phase 2: Claude Code -> tap-pre -> MaaS
#   make keyorder-up                            # Phase 4: Claude Code -> tap-pre -> APISIX -> tap-post
#
# Usage: scenarios/session_script.sh <2a|2b> [label]
#   2a  built-in tools only (--strict-mcp-config, no MCP server)
#   2b  + the lab-tfs MCP server (scenarios/mcp/lab_tfs.py, stdio); "mcp:" prompts included
#
# Prompts: scenarios/prompts/keyorder-session.txt, the first one starts a new session, the
# others use --continue (KEYORDER_CONTINUE=resume: --resume <session id> instead).
# After every prompt: tokens used by the session so far (from logs/raw/pre) and a key-order
# check (tap/keyorder.py). Stops with exit 3 on any type 1 drift (to be reported at once)
# and with exit 4 when the session passes KEYORDER_TOKEN_LIMIT input tokens (default 3M).
# Sessions are listed in logs/keyorder/sessions.jsonl; report in reports/keyorder-<label>-<run>.md.
set -euo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
[[ $# -ge 1 && "$1" =~ ^2[ab]$ ]] || { sed -n 2,18p "$0"; exit 2; }
VARIANT="$1"; LABEL="${2:-$1}"
DEV="${KEYORDER_DEV:-dev_alice}"
LIMIT="${KEYORDER_TOKEN_LIMIT:-3000000}"
PROMPTS="${KEYORDER_PROMPTS:-$LAB/scenarios/prompts/keyorder-session.txt}"
REPO="${KEYORDER_REPO:-$LAB/sample-repo-keyorder}"
CONTINUE="${KEYORDER_CONTINUE:-continue}"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
STATE="$LAB/logs/keyorder"
mkdir -p "$STATE" "$LAB/reports"
export PATH="$HOME/.local/bin:$PATH"

# Where tap-pre sends requests (recorded with the session).
env_of() { docker inspect maas-cache-lab-tap-pre-1 --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
             | sed -n "s/^$1=//p"; }
UPSTREAM="$(env_of TAP_UPSTREAM)"; TAP_MODE="$(env_of TAP_MODE)"
[[ -n "$UPSTREAM" ]] || { echo "error: tap-pre is not running (make keyorder-up)" >&2; exit 2; }

# Private copy of sample-repo, reset to the same commit before every session.
BASE_COMMIT="$(git -C "$LAB/sample-repo" rev-parse HEAD)"
[[ -d "$REPO/.git" ]] || git clone -q "$LAB/sample-repo" "$REPO"
git -C "$REPO" checkout -q --detach "$BASE_COMMIT"
git -C "$REPO" reset -q --hard "$BASE_COMMIT"
git -C "$REPO" clean -qfd

ALLOWED="Read,Grep,Glob,Edit,MultiEdit,Write,TaskCreate,TaskUpdate,TaskGet,TaskList,Agent"
ALLOWED+=",Bash(git diff:*),Bash(git status:*),Bash(git log:*),Bash(ls:*),Bash(wc:*),Bash(grep:*)"
MCP_ARGS=(--strict-mcp-config)
if [[ "$VARIANT" == 2b ]]; then
  MCP_CFG="$STATE/mcp-lab-tfs-$RUN_ID.json"
  TFS_STATE="$STATE/lab-tfs-state-$RUN_ID.json"
  rm -f "$TFS_STATE"
  printf '{"mcpServers":{"lab-tfs":{"command":"python3","args":["%s"],"env":{"LAB_TFS_STATE":"%s"}}}}\n' \
    "$LAB/scenarios/mcp/lab_tfs.py" "$TFS_STATE" > "$MCP_CFG"
  MCP_ARGS+=(--mcp-config "$MCP_CFG")
  ALLOWED+=",mcp__lab-tfs"
fi

mapfile -t LINES < <(grep -v '^[[:space:]]*\(#\|$\)' "$PROMPTS")
TURNS=()
for line in "${LINES[@]}"; do
  if [[ "$line" == mcp:* ]]; then
    [[ "$VARIANT" == 2b ]] && TURNS+=("${line#mcp: }")
  else
    TURNS+=("$line")
  fi
done
N=${#TURNS[@]}
VERSION="$(claude --version 2>/dev/null | head -1)"
# Rough upper estimate: ~4 requests per prompt, ~35K input tokens per request (system +
# tools ~20K plus a growing conversation); the real count is checked after every prompt.
echo "keyorder session: variant $VARIANT, label $LABEL, $N prompts, Claude Code $VERSION"
echo "  path: Claude Code -> tap-pre (TAP_MODE=${TAP_MODE:-passthrough}) -> $UPSTREAM; repo $REPO @ ${BASE_COMMIT:0:12}"
echo "  estimate: ~$((N * 4 * 35)) K input tokens (limit $((LIMIT / 1000)) K per session)"

# tokens <session>: input+cache_read+cache_creation and request count so far (tap-pre files)
stats() {
  python3 - "$LAB/logs/raw/pre" "$1" <<'EOF'
import json, sys
from pathlib import Path
total = n = 0
for p in Path(sys.argv[1]).glob("*.json"):
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        continue
    if m.get("session") != sys.argv[2]:
        continue
    u = m.get("usage") or {}
    n += 1
    total += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
print(n, total)
EOF
}
# type1 <session>: type 1 transitions and transitions compared (tap-pre source)
type1() {
  python3 "$LAB/tap/keyorder.py" --raw "$LAB/logs/raw" --session "$1" --out /dev/null \
          --json "$STATE/check.json" 2>/dev/null
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["sources"].get("pre") or {}
print((s.get("tools_change") or {}).get("key_order", 0), s.get("comparable", 0), s.get("any_drift", 0))' "$STATE/check.json"
}

SESSION=""; STARTED="$(date -u +%FT%TZ)"; DONE=0; STOP=""; NEW_SESSIONS=0
for ((i = 0; i < N; i++)); do
  args=(-p --output-format json --permission-mode acceptEdits "${MCP_ARGS[@]}" --allowedTools "$ALLOWED")
  if [[ -n "$SESSION" ]]; then
    if [[ "$CONTINUE" == resume ]]; then args+=(--resume "$SESSION"); else args+=(--continue); fi
  fi
  t0=$(date +%s)
  out="$(SAMPLE_REPO="$REPO" bash "$LAB/scripts/run-claude.sh" "$DEV" P "${args[@]}" <<< "${TURNS[$i]}" 2>/dev/null \
         | tail -1)" || true
  read -r sid is_err nturns <<< "$(python3 -c 'import json,sys
try:
    o = json.loads(sys.stdin.read())
    print(o.get("session_id", "-"), o.get("is_error"), o.get("num_turns"))
except ValueError:
    print("-", "parse-error", "-")' <<< "$out")"
  if [[ -z "$SESSION" ]]; then
    SESSION="$sid"
  elif [[ "$sid" != "$SESSION" ]]; then
    NEW_SESSIONS=$((NEW_SESSIONS + 1))
    echo "  warning: prompt $((i + 1)) ran in session $sid, not $SESSION" >&2
  fi
  DONE=$((i + 1))
  read -r nreq tokens <<< "$(stats "$SESSION")"
  read -r t1 cmp drift <<< "$(type1 "$SESSION")"
  echo "  prompt $DONE/$N: $(( $(date +%s) - t0 ))s, turns=$nturns error=$is_err | session requests=$nreq" \
       "input tokens=$((tokens / 1000))K | compared=$cmp type1=$t1 any-drift=$drift"
  if [[ "$t1" != 0 ]]; then STOP="type1"; break; fi
  if (( tokens > LIMIT )); then STOP="token-limit"; break; fi
done

python3 - "$STATE/sessions.jsonl" <<EOF
import json, sys
rec = {"run_id": "$RUN_ID", "label": "$LABEL", "variant": "$VARIANT", "session": "$SESSION",
       "claude_version": "$VERSION", "tap_pre_upstream": "$UPSTREAM", "tap_pre_mode": "${TAP_MODE:-passthrough}",
       "repo_commit": "$BASE_COMMIT", "prompts_done": $DONE, "prompts_total": $N,
       "sessions_started_by_continue": $NEW_SESSIONS, "started": "$STARTED",
       "ended": "$(date -u +%FT%TZ)", "stop": "$STOP" or None}
open(sys.argv[1], "a").write(json.dumps(rec) + "\n")
EOF
REPORT="$LAB/reports/keyorder-$LABEL-$RUN_ID.md"
python3 "$LAB/tap/keyorder.py" --raw "$LAB/logs/raw" --session "$SESSION" --out "$REPORT" 2>/dev/null
echo "session $SESSION: report $REPORT"
sed -n '/^| source/,/^$/p' "$REPORT" | head -4
case "$STOP" in
  type1) echo "STOP: type 1 key-order drift found (see report)"; exit 3 ;;
  token-limit) echo "STOP: session passed $LIMIT input tokens"; exit 4 ;;
esac
