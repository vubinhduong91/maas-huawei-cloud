#!/usr/bin/env bash
# Claude Code hook (SessionStart / SessionEnd): sync this project's Claude memory directory,
# a separate git repo with a private remote, between the PCs this project is worked on.
# Commits local changes, rebases on the remote, pushes. Never fails the session.
M="$HOME/.claude/projects/D--projects-spinning-seal/memory"
warn() { printf '{"systemMessage": "memory-sync: %s"}\n' "$1"; exit 0; }
[ -d "$M/.git" ] || warn "$M is not a git repo (clone the private memory repo there)"
cd "$M" || exit 0
git add -A
git diff --cached --quiet || git commit -q -m "memory: $(hostname) $(date '+%Y-%m-%d %H:%M')" || warn "commit failed"
if ! git pull -q --rebase 2>/dev/null; then
    git rebase --abort 2>/dev/null
    warn "pull failed (offline or conflict) - resolve in $M"
fi
git push -q 2>/dev/null || warn "push failed (offline?) - will retry next session"
exit 0
