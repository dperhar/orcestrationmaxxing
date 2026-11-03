#!/usr/bin/env bash
# install.sh: install the whole orcestrationmaxxing pipeline for OMP + Orca.
#
#   bash install.sh            dry run: print the plan, change nothing
#   bash install.sh --apply    install
#   bash install.sh --apply --force   also overwrite installed skills/agents that differ
#
# Installs:
#   1. orca-lifecycle.py + the orca-triage OMP extension   (bin/install-orca-triage.sh)
#   2. skills/*  -> ~/.omp/agent/managed-skills/<name>/
#   3. agents/*  -> ~/.omp/agent/agents/
#   4. the skill-router OMP extension: generates registry.ts from the skills on this
#      machine, then deploys omp/skill-router -> ~/.omp/agent/extensions/skill-router
# Third-party skills are not copied. The script prints their upstream install commands.
# Writes no secrets and never deletes anything.
set -euo pipefail

APPLY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: $(basename "$0") [--apply] [--force]" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_HOME="${HOME:?HOME is not set}"
SKILLS_DEST="$HOST_HOME/.omp/agent/managed-skills"
AGENTS_DEST="$HOST_HOME/.omp/agent/agents"
PY="${PYTHON:-/usr/bin/python3}"

say() { printf '%s\n' "$*"; }
run() { if [ "$APPLY" -eq 1 ]; then "$@"; else say "would run: $*"; fi; }

# Copy one file unless the installed copy differs and --force is not set.
place() {
  local src="$1" dest="$2"
  if [ -f "$dest" ] && ! cmp -s "$src" "$dest" && [ "$FORCE" -eq 0 ]; then
    say "keep (differs; --force to overwrite): $dest"
    return
  fi
  if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
    say "same: $dest"
    return
  fi
  if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$(dirname "$dest")"
    cp -p "$src" "$dest"
    say "installed: $dest"
  else
    say "would copy: $src -> $dest"
  fi
}

[ "$APPLY" -eq 1 ] || say "DRY RUN (nothing changed). Re-run with --apply to install."

say "== 1. lifecycle CLI and orca-triage extension"
if [ "$APPLY" -eq 1 ]; then bash "$REPO/bin/install-orca-triage.sh" --apply; else bash "$REPO/bin/install-orca-triage.sh"; fi

say "== 2. skills"
for dir in "$REPO"/skills/*/; do
  name="$(basename "$dir")"
  while IFS= read -r -d '' f; do
    place "$f" "$SKILLS_DEST/$name/${f#"$dir"}"
  done < <(find "$dir" -type f -print0)
done

say "== 3. agents"
for f in "$REPO"/agents/*.md; do
  place "$f" "$AGENTS_DEST/$(basename "$f")"
done

say "== 4. skill-router extension"
if [ "$APPLY" -eq 1 ]; then
  ORCH_REPO_DIR="$REPO" "$PY" "$REPO/bin/refresh-skill-router-registry.py" --write
else
  ORCH_REPO_DIR="$REPO" "$PY" "$REPO/bin/refresh-skill-router-registry.py" --check >/dev/null 2>&1 || true
  say "would run: ORCH_REPO_DIR=$REPO $PY bin/refresh-skill-router-registry.py --write"
fi

cat <<'EOF'
== 5. third-party skills (install from upstream; see THIRD_PARTY.md)
  npx skills add mattpocock/skills                 # grilling, grill-me, wayfinder, to-spec, to-tickets, implement, code-review
  npx skills add stablyai/orca                     # orchestration, orca-cli
  git clone https://github.com/tsergeytovarov/super-puper-powers ~/.config/opencode/super-puper-powers
  omp plugin link ~/.config/opencode/super-puper-powers   # plan-writing, plan-review, TDD, debugging, verification
After installing them, run again so the router registry picks them up:
  bash install.sh --apply
Then, in the main checkout of each project:
  L=~/Library/Scripts/orcestrationmaxxing/orca-lifecycle.py
  /usr/bin/python3 "$L" project-add --repo-path .
  /usr/bin/python3 "$L" triage-here --dir .
Restart OMP there.
EOF
