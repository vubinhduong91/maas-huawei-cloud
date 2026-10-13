#!/usr/bin/env bash
# Compress old lab logs into logs/archive/ and remove the originals once the archive checks out.
#   - logs/raw and logs/tap files last modified before BEFORE -> <dir>-before-<BEFORE>.tar.zst
#   - logs/apisix/requests.log (never rotated) -> apisix-requests-<now>.log.zst, then truncated
#     in place (copy-truncate: APISIX keeps its file handle; lines written in between are lost)
# Restore: tar -I zstd -xf logs/archive/<file>.tar.zst   (paths are relative to the lab root)
# Usage: bash scripts/archive-logs.sh [BEFORE=YYYY-MM-DD, default today]
set -euo pipefail
cd "$(dirname "$0")/.."
BEFORE="${1:-$(date +%F)}"
TAG="${BEFORE//-/}"
mkdir -p logs/archive

for dir in logs/raw logs/tap; do
  [[ -d $dir ]] || continue
  name="$(basename "$dir")-before-$TAG"
  list="logs/archive/$name.list"
  find "$dir" -type f ! -newermt "$BEFORE" > "$list"
  n=$(wc -l < "$list")
  if [[ $n == 0 ]]; then echo "$dir: nothing before $BEFORE"; rm -f "$list"; continue; fi
  out="logs/archive/$name.tar.zst"
  [[ -e $out ]] && { echo "$out exists, not overwriting"; exit 1; }
  tar -I 'zstd -T0 -10' -cf "$out" -T "$list"
  got=$(tar -I zstd -tf "$out" | wc -l)
  [[ $got == "$n" ]] || { echo "$out: $got entries, expected $n; originals kept"; exit 1; }
  xargs -d '\n' rm -f < "$list"
  if [[ $dir == logs/tap ]]; then find "$dir" -mindepth 1 -type d -empty -delete; fi   # session dirs
  echo "$dir: $n files -> $out ($(du -h "$out" | cut -f1))"
done

log=logs/apisix/requests.log
if [[ -s $log ]]; then
  out="logs/archive/apisix-requests-$(date +%Y%m%d-%H%M%S).log.zst"
  zstd -q -T0 -10 -o "$out" "$log"
  zstd -q -t "$out"
  : > "$log"
  echo "$log -> $out ($(du -h "$out" | cut -f1))"
fi
du -sh logs
