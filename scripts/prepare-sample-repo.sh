#!/usr/bin/env bash
# Prepare sample-repo/: Reaparr pinned to one commit, plus the lab CLAUDE.md committed on
# branch "lab" so `git status` is clean (Claude Code puts git status in its prompt).
# Usage: scripts/prepare-sample-repo.sh [--force]   (--force re-creates sample-repo/)
set -euo pipefail
cd "$(dirname "$0")/.."

REPO_URL="https://github.com/Reaparr/Reaparr.git"
COMMIT="3f0246356944e622407cb9c57328d808bbc9da42"   # branch dev, 2026-10-10
DIR="sample-repo"
GIT=(git -C "$DIR" -c user.name=lab -c user.email=lab@localhost -c commit.gpgsign=false)

if [[ -d "$DIR/.git" && "${1:-}" != "--force" ]]; then
  echo "sample-repo: already present at $("${GIT[@]}" rev-parse --short HEAD) (use --force to re-create)"
  exit 0
fi
rm -rf "$DIR"
mkdir -p "$DIR"
git -C "$DIR" init -q -b lab
"${GIT[@]}" remote add origin "$REPO_URL"
"${GIT[@]}" fetch -q --depth 1 origin "$COMMIT"
"${GIT[@]}" checkout -q -B lab FETCH_HEAD
cp sample/CLAUDE.md "$DIR/CLAUDE.md"
"${GIT[@]}" add CLAUDE.md
"${GIT[@]}" commit -q -m "lab: add CLAUDE.md"
echo "sample-repo: Reaparr ${COMMIT:0:12} + CLAUDE.md on branch lab ($("${GIT[@]}" rev-parse --short HEAD))"
echo "files: $("${GIT[@]}" ls-files | wc -l)  (cs: $("${GIT[@]}" ls-files '*.cs' | wc -l), vue: $("${GIT[@]}" ls-files '*.vue' | wc -l), ts: $("${GIT[@]}" ls-files '*.ts' | wc -l))"
