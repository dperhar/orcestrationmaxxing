#!/usr/bin/env bash
# install-orca-triage.sh
# Installs the orca-triage OMP extension and the orca-lifecycle.py registry CLI.
#
#   bash install-orca-triage.sh            dry run: print the plan, change nothing
#   bash install-orca-triage.sh --apply    copy the files
#
# Copies:
#   omp/orca-triage/{index.ts,policy.ts} -> ~/.omp/agent/extensions/orca-triage/
#   bin/orca-lifecycle.py                -> ~/Library/Scripts/orcestrationmaxxing/orca-lifecycle.py (chmod 755)
#
# Writes no secrets. Does not register a triage directory; run
# `orca-lifecycle.py triage-here` from the triage worktree for that.
set -euo pipefail

APPLY=0
case "${1:-}" in
  "") ;;
  --apply) APPLY=1 ;;
  -h|--help)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") [--apply]" >&2
    exit 2
    ;;
esac
[ "$#" -le 1 ] || { echo "usage: $(basename "$0") [--apply]" >&2; exit 2; }

HOST_HOME="${HOME:?HOME is not set}"
HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SRC_EXT_DIR="$HARNESS_DIR/omp/orca-triage"
SRC_LIFECYCLE="$HARNESS_DIR/bin/orca-lifecycle.py"
DEST_EXT_DIR="$HOST_HOME/.omp/agent/extensions/orca-triage"
DEST_LIFECYCLE_DIR="$HOST_HOME/Library/Scripts/orcestrationmaxxing"
DEST_LIFECYCLE="$DEST_LIFECYCLE_DIR/orca-lifecycle.py"
EXT_FILES=(index.ts policy.ts)

missing=0
for f in "${EXT_FILES[@]}"; do
  if [ ! -f "$SRC_EXT_DIR/$f" ]; then
    echo "missing source: $SRC_EXT_DIR/$f" >&2
    missing=1
  fi
done
if [ ! -f "$SRC_LIFECYCLE" ]; then
  echo "missing source: $SRC_LIFECYCLE" >&2
  missing=1
fi
[ "$missing" -eq 0 ] || exit 1

if [ "$APPLY" -eq 0 ]; then
  echo "DRY RUN (nothing changed). Re-run with --apply to install."
  echo "mkdir -p $DEST_EXT_DIR"
  for f in "${EXT_FILES[@]}"; do
    echo "copy $SRC_EXT_DIR/$f -> $DEST_EXT_DIR/$f"
  done
  echo "mkdir -p $DEST_LIFECYCLE_DIR"
  echo "copy $SRC_LIFECYCLE -> $DEST_LIFECYCLE (chmod 755)"
  exit 0
fi

mkdir -p "$DEST_EXT_DIR" "$DEST_LIFECYCLE_DIR"
for f in "${EXT_FILES[@]}"; do
  cp -f "$SRC_EXT_DIR/$f" "$DEST_EXT_DIR/$f"
  echo "installed $DEST_EXT_DIR/$f"
done
cp -f "$SRC_LIFECYCLE" "$DEST_LIFECYCLE"
chmod 755 "$DEST_LIFECYCLE"
echo "installed $DEST_LIFECYCLE"
echo "Next: run '$DEST_LIFECYCLE triage-here' in the triage worktree, then restart OMP."
