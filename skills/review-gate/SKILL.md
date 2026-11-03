---
name: review-gate
description: "Read-only whole-diff reviewer gate run after verification and before commit. Use when implementation work is verified and about to be committed, or when the ROUTE_DIRECTIVE names skill://review-gate."
---

# Review gate (read-only, before commit)

Runs after implementation + normal verification, before the commit. The reviewer
finds defects; it never edits. You fix, re-verify, re-review.

## When it applies

Applies when the turn produced an implementation diff. Skip when there is no
diff (planning, spec, ticket generation, research) or when the turn's own
primary skill is itself a review skill — a reviewer never gates itself.

## Loop

```
implement → verify → reviewer → fix + TDD → verify → reviewer → commit
```

Re-run after fixes. A pass that produced findings is not a pass, and fixes
routinely introduce new defects.

## 1. Build the artifact (fail closed)

The reviewer has no shell, so hand it a file. Never `git add` — the index stays
as the user left it; `--no-index` reads the worktree without staging.

```bash
set -euo pipefail
ART="$(mktemp -t review-diff).patch"   # outside the worktree, not a repo path
git diff HEAD > "$ART"
UNTRACKED="$(mktemp)"
git ls-files --others --exclude-standard -z | while IFS= read -r -d '' p; do
  # `git ls-files --others` collapses a wholly-untracked directory into a single
  # entry. `git diff --no-index` on a directory does not walk it — it prints
  # "Could not access <dir>/null" and contributes nothing, so a brand-new
  # subtree would silently escape review. Expand directories to their files.
  if [ -d "$p" ]; then
    # Prune `.git`: an untracked entry is often a nested repository, and its
    # object store would bury the review in thousands of binary blobs.
    find "$p" -name .git -prune -o -type f -print0
  else
    printf '%s\0' "$p"
  fi
done > "$UNTRACKED"
while IFS= read -r -d '' f; do
  case "$f" in *review-diff*.patch) continue ;; esac   # never diff a review artifact
  git diff --no-index /dev/null "$f" >> "$ART" || [ $? -eq 1 ] || exit 1
done < "$UNTRACKED"
rm -f "$UNTRACKED"
[ -s "$ART" ] || { echo "review-gate: empty artifact; nothing to review" >&2; exit 1; }
echo "$ART"   # absolute path to hand the reviewer
```

Three protections, all needed:

- The artifact lives in a fresh `mktemp` file, never a fixed repository path. A
  repo-controlled path could be a symlink, and `>` would follow it and truncate
  its target before any guard ran.
- The loop skips `*review-diff*.patch`. A stray artifact left inside the worktree
  by an earlier run still shows up in `git ls-files --others`, and diffing it
  would inflate the review with a meta-diff of a previous review.
- Untracked directories are expanded to their files. `git ls-files --others`
  reports a wholly-new subtree as one directory entry, and `--no-index` on a
  directory reviews nothing — an entire new feature could ship unreviewed.

Read the untracked list from a temp file rather than `< <(...)`: process
substitution can hang in some agent shells, which would stall the mandatory
pre-commit gate.

Untracked files are appended per-file because plain `git diff` omits them, and a
pre-commit review that misses new code is worthless. `--no-index` exits 1 when
files differ (expected); anything higher is a real failure and must abort rather
than hand over a partial artifact.

## 2. Credential guard (blocking)

The artifact copies source out of this process, so screen it before dispatch.
Prefer a real scanner; fall back to shape-matching only when none is installed.

```bash
SCAN=""
for c in gitleaks; do command -v "$c" >/dev/null 2>&1 && SCAN="$c" && break; done

if [ -n "$SCAN" ]; then
  # 9 = leaks found, 0 = clean, anything else = the scanner itself failed.
  gitleaks detect --no-git --source "$ART" --redact --exit-code 9 \
    --report-format json --report-path "$ART.leaks.json" >/dev/null 2>&1
  rc=$?
  case "$rc" in
    0) : ;;
    9) echo "review-gate: gitleaks found credentials; dispatch blocked" >&2
       echo "  findings: $ART.leaks.json (values redacted)" >&2
       exit 1 ;;
    *) echo "review-gate: gitleaks failed (exit $rc); dispatch blocked" >&2
       exit 1 ;;
  esac
else
  # Fallback: token prefixes and key blocks, never the words "secret" or
  # "api_key", which match ordinary code.
  PAT='(BEGIN [A-Z ]*PRIVATE KEY|xox[abp]-[0-9A-Za-z-]{10,}|gh[pousr]_[0-9A-Za-z]{30,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})'
  if grep -qE "$PAT" "$ART"; then
    echo "review-gate: credential-shaped content in artifact; dispatch blocked" >&2
    exit 1
  fi
  echo "review-gate: no scanner installed; shape-matching only" >&2
fi
```

Install the scanner once with `brew install gitleaks`; the fallback exists so a
machine without it still gets *some* screening, not so it can be relied on.

Keep `grep` inside the `if` condition — under `set -e` a bare `grep -qE` exits
the script on its clean-artifact return of 1. Capture `gitleaks`' status into
`rc` on the next line rather than testing `$?` after an `if`, where it would
report the branch instead of the command. A scanner error blocks dispatch: a
check that cannot run is not a pass. `--redact` keeps secret values out of both
the report and the transcript.

On a hit: abort, remediate the credential locally (remove it, move it to the
keychain, gitignore the file), then regenerate the full artifact. Do not ship a
trimmed artifact to get a green light, and never name the file for the reviewer
to read instead — that sends the credential anyway. If some exclusion is
genuinely unavoidable, the verdict is **partial**, not clean.

gitleaks allowlists documented example values (`AKIAIOSFODNN7EXAMPLE` and
friends), so a test fixture using those will read clean — that is correct, not a
miss. Real high-entropy values are caught by its `generic-api-key` rule, which
the old shape-matching guard had no equivalent for.

## 3. Dispatch

Spawn the `reviewer` agent with the absolute artifact path, the authoritative
plan/spec path, the branch base, and the acceptance criteria. The spec matters as
much as the diff: a diff-only reviewer cannot see "this used to route to X".

## 4. Handle findings

The reviewer may flag a minimal test tied to a concrete defect; you decide and
you write it, under the project's normal TDD rules. Decline broad "add coverage"
pressure. Findings on code that should not exist are still findings — prefer
deleting over patching. Fix at the source, re-verify, re-review.

Commit only when the current artifact returns no Critical/Important findings.
Report the verdict and the tier it ran at.
