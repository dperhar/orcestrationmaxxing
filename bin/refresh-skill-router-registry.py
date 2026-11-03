#!/usr/bin/env python3
"""Refresh OMP skill-router registry.ts from on-disk skill trees.

Canonical source of the skill-router package is this repository's
`omp/skill-router/`. The live package under
`$HOME/.omp/agent/extensions/skill-router/` is generated deployment state
only and is never hand-edited.

`registry.ts` is wholly generated; every other router source is hand-authored.

Run it after installing or updating skills so auto-summon paths stay current.
Does NOT rewrite intent-map.ts (hand rules).

Environment:
  ORCH_REPO_DIR        repository that holds omp/skill-router (default: this script's repository)
  ORCH_SKILLS_DIR      repository whose skills/ folder is scanned (default: ORCH_REPO_DIR)
  SKILL_ROUTER_MIN_ROWS  refuse to write a registry smaller than this (default 5)

Usage:
  refresh-skill-router-registry.py --check   [--test] [--json-summary PATH]
  refresh-skill-router-registry.py --write   [--test] [--json-summary PATH]
  refresh-skill-router-registry.py --dry-run            # deprecated: --check
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat as stat_mod
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HOST_HOME = Path(os.environ.get("HOME", str(Path.home())))


CANONICAL_REL = Path("omp") / "skill-router"


def repo_dir() -> Path:
    explicit = os.environ.get("ORCH_REPO_DIR")
    if explicit:
        return Path(explicit)

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / CANONICAL_REL).is_dir():
            return candidate

    return Path(__file__).resolve().parents[1]


def canonical_pkg() -> Path:
    """Repository-owned router package (the source of truth)."""
    return repo_dir() / CANONICAL_REL


def live_pkg() -> Path:
    """Generated deployment state. Never hand-edited."""
    return HOST_HOME / ".omp/agent/extensions/skill-router"


DEFAULT_REGISTRY = live_pkg() / "registry.ts"

# A scan under this floor means the skill trees were unavailable (unmounted HOME,
# partial checkout) rather than genuinely shrunk. Writing such a scan once
# truncated a live registry to a handful of rows.
MIN_REGISTRY_ROWS = int(os.environ.get("SKILL_ROUTER_MIN_ROWS", "5"))

# A partial scan can clear MIN_REGISTRY_ROWS and still silently drop skills
# (observed: 226 -> 186 when the scan roots were repointed). Block a write that
# loses this fraction of existing rows unless --allow-shrink is passed.
MAX_REGISTRY_SHRINK = 0.10


# --- tree-v1 canonical directory digest ---

TREE_HEADER = b"OM-TREE\0v1\0"

# macOS renameatx_np flag from <stdio.h>. Atomically exchanges two paths on
# the same filesystem; both paths must already exist.
RENAME_SWAP = 0x00000002


class SpecialFileError(RuntimeError):
    """A tree contained something other than a regular file or symlink."""


def tree_v1(root: Path) -> str:
    """Digest every regular file and symlink below `root`.

    No directory records. Entries sort by unsigned relative UTF-8 path bytes.
    Each entry is
    uint32be(path_len) || path || type_byte || uint32be(mode & 0o777)
    || uint64be(data_len) || data
    where data is file bytes or the symlink target bytes.
    """
    entries: list[tuple[bytes, Path]] = []
    for path in root.rglob("*"):
        st = path.lstat()
        if stat_mod.S_ISLNK(st.st_mode):
            pass
        elif stat_mod.S_ISDIR(st.st_mode):
            continue
        elif not stat_mod.S_ISREG(st.st_mode):
            raise SpecialFileError(str(path))
        entries.append((path.relative_to(root).as_posix().encode("utf-8"), path))

    h = hashlib.sha256()
    h.update(TREE_HEADER)
    for rel, path in sorted(entries, key=lambda item: item[0]):
        st = path.lstat()
        if stat_mod.S_ISLNK(st.st_mode):
            type_byte, data = b"L", os.readlink(path).encode("utf-8")
        else:
            type_byte, data = b"F", path.read_bytes()
        h.update(struct.pack(">I", len(rel)))
        h.update(rel)
        h.update(type_byte)
        h.update(struct.pack(">I", st.st_mode & 0o777))
        h.update(struct.pack(">Q", len(data)))
        h.update(data)
    return h.hexdigest()

# skill: + // avoided as a single literal in some shells; safe inside this file.
SKILL_URI = "skill:" + "//"

ALIASES = {
    "gstack-autoplan": "autoplan",
}

PRIMARY_GROUPS = {
    "work-routing": "work-routing",
    "grill": "grilling",
    "tdd": "test-driven-development",
    "debug": "systematic-debugging",
    "verify": "verification-before-completion",
    "plan-review": "plan-review",
    "plan-writing": "plan-writing",
    "autoplan": "autoplan",
    "wayfinder": "wayfinder",
    "to-tickets": "to-tickets",
    "to-spec": "to-spec",
    "implement": "implement",
    "orchestration": "orchestration",
    "spp-orchestrator": "using-super-puper-powers",
}

GROUP_OF = {
    "work-routing": "work-routing",
    "grilling": "grill",
    "grill-me": "grill",
    "grill-with-docs": "grill",
    "test-driven-development": "tdd",
    "tdd": "tdd",
    "systematic-debugging": "debug",
    "investigate": "debug",
    "diagnosing-bugs": "debug",
    "gstack-openclaw-investigate": "debug",
    "verification-before-completion": "verify",
    "plan-review": "plan-review",
    "plan-writing": "plan-writing",
    "autoplan": "autoplan",
    "wayfinder": "wayfinder",
    "to-tickets": "to-tickets",
    "to-spec": "to-spec",
    "implement": "implement",
    "orchestration": "orchestration",
    "using-super-puper-powers": "spp-orchestrator",
}

HIDDEN = {
    "grill-me",
    "wayfinder",
    "to-tickets",
    "to-spec",
    "implement",
    "grill-with-docs",
    "ask-matt",
    "handoff",
    "claude-handoff",
    "loop-me",
    "teach",
    "wizard",
    "setup-matt-pocock-skills",
    "triage",
    "ubiquitous-language",
    "writing-beats",
    "writing-fragments",
    "writing-great-skills",
    "writing-shape",
    "edit-article",
    "improve-codebase-architecture",
}

DEAD = {
    # pack collision losers only — do NOT kill uncovered primaries for lack of intent
    "tdd",
    "investigate",
    "gstack-openclaw-investigate",
}

SKIP_PARTS = {"node_modules", ".git"}

# Backup/disabled siblings must never become the active source. `.` sorts before
# `/`, so the equal-rank path tiebreak below would otherwise let
# `research/arxiv.bak/SKILL.md` beat `research/arxiv/SKILL.md`, and the router
# reads sourcePath — it would serve the stale body indefinitely.
SKIP_DIR_SUFFIXES = (".bak", ".old", ".orig", ".disabled", ".save", "~")

# Some trees (notably ~/gstack) ship one skill once per agent host:
# .agents/, .cursor/, .hermes/, .opencode/ ... The bodies differ — each hardcodes
# its own host paths — so the winner is a real choice, not a tie. This harness is
# OMP, which reads `.agents`, so prefer that host explicitly instead of letting a
# lexicographic path comparison decide it by accident.
PREFERRED_HOST_DIR = ".agents"
HOST_DIR_MARKERS = (
    ".agents",
    ".claude",
    ".codex",
    ".cursor",
    ".factory",
    ".gbrain",
    ".gemini",
    ".hermes",
    ".kiro",
    ".openclaw",
    ".opencode",
    ".slate",
)


def host_penalty(path: Path) -> int:
    """0 when a path is host-neutral or on the preferred host, 1 otherwise."""
    for part in path.parts:
        if part in HOST_DIR_MARKERS:
            return 0 if part == PREFERRED_HOST_DIR else 1
    return 0

def skills_repo_dir() -> Path:
    """Root for scanning SKILL trees.

    Separate from `repo_dir()`, which locates the canonical router package:
    refreshing a worktree's package must still scan the real skill install.
    Override with ORCH_SKILLS_DIR.
    """
    explicit = os.environ.get("ORCH_SKILLS_DIR")
    if explicit:
        return Path(explicit)
    return repo_dir()


def roots() -> list[tuple[Path, int, str]]:
    return [
        (HOST_HOME / ".omp/agent/managed-skills", 10, "managed"),
        (skills_repo_dir() / "skills", 15, "repo"),
        (HOST_HOME / ".agents/skills", 20, "agents"),
        (HOST_HOME / ".config/opencode/super-puper-powers/skills", 30, "sdp"),
        (HOST_HOME / ".omp/plugins/node_modules", 35, "omp-plugins"),
        (HOST_HOME / ".claude/skills", 40, "claude"),
        (HOST_HOME / "gstack", 50, "gstack"),
    ]


def parse_name_dmi(text: str) -> tuple[str | None, bool]:
    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---", text)
    if not m:
        return None, False
    body = m.group(1)
    name = None
    nm = re.search(r'^name:\s*["\']?([^"\'\n]+)["\']?\s*$', body, re.M)
    if nm:
        name = nm.group(1).strip()
    dmi = bool(
        re.search(r"^disable-model-invocation:\s*true\s*$", body, re.M | re.I)
    )
    return name, dmi


def scan() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for root, rank, src in roots():
        if not root.exists():
            continue
        # Sort so equal-rank duplicates resolve identically on every run.
        # ~/gstack ships nine copies of some skills at one rank; leaving the
        # winner to filesystem order produced byte-different registries and a
        # permanent false "canonical registry.ts stale".
        for skill_md in sorted(root.rglob("SKILL.md")):
            if any(part in SKIP_PARTS for part in skill_md.parts):
                continue
            if any(
                part.endswith(SKIP_DIR_SUFFIXES) for part in skill_md.parent.parts
            ):
                continue
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parent = skill_md.parent.name
            name, dmi = parse_name_dmi(text)
            if not name:
                name = parent
            name = ALIASES.get(name, name)
            name = ALIASES.get(parent, name)
            path = str(skill_md.resolve())
            penalty = host_penalty(skill_md)
            prev = found.get(name)
            if prev and prev["rank"] < rank:
                continue
            if prev and prev["rank"] == rank:
                # Prefer this harness's host, then the smallest path so the
                # winner never depends on filesystem traversal order.
                if (prev.get("host_penalty", 0), prev["path"]) <= (penalty, path):
                    continue
            found[name] = {
                "name": name,
                "path": path,
                "rank": rank,
                "host_penalty": penalty,
                "dmi": dmi,
                "chars": len(text),
                "src": src,
            }

    return found


def family_for(name: str) -> str:
    if name == "work-routing":
        return "work-routing"
    if name in {
        "grilling",
        "grill-me",
        "wayfinder",
        "to-tickets",
        "to-spec",
        "implement",
    }:
        return "matt"
    if name in {
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
        "plan-review",
        "plan-writing",
        "subagent-driven-development",
    }:
        return "sdp"
    if name == "using-super-puper-powers":
        return "spp"
    if name == "autoplan":
        return "gstack"
    if name == "orchestration":
        return "ops"
    if name.startswith("nmt-"):
        return "nmt"
    if name.startswith("ajtbd-"):
        return "other"
    if name.startswith("plan-"):
        return "gstack"
    if name.startswith("design-"):
        return "gstack"
    return "other"


def role_for(name: str, dmi: bool) -> str:
    group = GROUP_OF.get(name, name)
    if name in DEAD:
        return "dead"
    if name in HIDDEN or dmi:
        if name in HIDDEN or dmi:
            # keep model-selectable adjuncts that happen to be dmi false
            if name in HIDDEN or dmi:
                if name in HIDDEN or (
                    dmi
                    and name
                    in {
                        "grill-me",
                        "wayfinder",
                        "to-tickets",
                        "to-spec",
                        "implement",
                    }
                ):
                    return "hidden-command"
    if dmi and name not in HIDDEN:
        return "hidden-command"
    if PRIMARY_GROUPS.get(group) == name:
        return "primary"
    if group in PRIMARY_GROUPS and PRIMARY_GROUPS[group] != name:
        return "adjunct"
    return "primary"


def load_path_for(name: str, meta: dict) -> str:
    if name == "grill-me":
        return f"{SKILL_URI}grilling"
    if name in {"wayfinder", "to-tickets", "to-spec", "implement"}:
        return meta["path"]
    if meta["chars"] > 40000:
        return meta["path"]
    if meta["rank"] <= 30:
        return f"{SKILL_URI}{name}"
    return meta["path"]


def build_rows(found: dict[str, dict]) -> list[dict]:
    rows = []
    for name, meta in sorted(found.items()):
        group = GROUP_OF.get(name, name)
        role = role_for(name, bool(meta["dmi"]))
        # Normalize hidden-command for known set even if dmi false on body skills
        if name in {
            "grill-me",
            "wayfinder",
            "to-tickets",
            "to-spec",
            "implement",
        }:
            role = "hidden-command"
        if name == "grilling":
            role = "primary"
        rows.append(
            {
                "name": name,
                "sourcePath": meta["path"],
                "providerRank": meta["rank"],
                "disableModelInvocation": bool(meta["dmi"]),
                "collisionGroup": group,
                "role": role,
                "ownerFamily": family_for(name),
                "loadPath": load_path_for(name, meta),
                "chars": meta["chars"],
            }
        )
    return rows


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def render_registry_ts(rows: list[dict]) -> str:
    """Clean renderer without the botched line above."""
    lines: list[str] = []
    lines.append(
        'export type SkillRole = "primary" | "adjunct" | "hidden-command" | "dead";'
    )
    lines.append("export type OwnerFamily =")
    lines.append(
        '  | "work-routing" | "nmt" | "gstack" | "sdp" | "spp" | "matt" | "ops" | "content" | "other";'
    )
    lines.append("")
    lines.append("export interface SkillRegistryRow {")
    lines.append("  name: string;")
    lines.append("  sourcePath: string;")
    lines.append("  providerRank: number;")
    lines.append("  disableModelInvocation: boolean;")
    lines.append("  collisionGroup: string;")
    lines.append("  role: SkillRole;")
    lines.append("  ownerFamily: OwnerFamily;")
    lines.append("  loadPath?: string;")
    lines.append("  /** skill body char count at inventory time */")
    lines.append("  chars?: number;")
    lines.append("}")
    lines.append("")
    lines.append(
        "/** Auto-generated by refresh-skill-router-registry.py — do not hand-edit rows. */"
    )
    lines.append("export const SKILL_REGISTRY: SkillRegistryRow[] = [")
    for r in rows:
        dmi = "true" if r["disableModelInvocation"] else "false"
        lines.append(
            "  { "
            f"name: '{esc(r['name'])}', "
            f"sourcePath: '{esc(r['sourcePath'])}', "
            f"providerRank: {r['providerRank']}, "
            f"disableModelInvocation: {dmi}, "
            f"collisionGroup: '{esc(r['collisionGroup'])}', "
            f"role: '{r['role']}', "
            f"ownerFamily: '{r['ownerFamily']}', "
            f"loadPath: '{esc(r['loadPath'])}', "
            f"chars: {r['chars']} "
            "},"
        )
    lines.append("];")
    lines.append("")
    lines.append(
        "export function primaryForGroup(group: string): SkillRegistryRow | undefined {"
    )
    lines.append(
        '  return SKILL_REGISTRY.find((r) => r.collisionGroup === group && r.role === "primary");'
    )
    lines.append("}")
    lines.append("")
    lines.append(
        "export function registryByName(name: string): SkillRegistryRow | undefined {"
    )
    lines.append("  return SKILL_REGISTRY.find((r) => r.name === name);")
    lines.append("}")
    lines.append("")
    lines.append("export function loadPathFor(name: string): string {")
    lines.append(f'  if (name === "grill-me") return "{SKILL_URI}grilling";')
    lines.append("  const r = registryByName(name);")
    lines.append(f"  if (!r) return `{SKILL_URI}${{name}}`;")
    lines.append("  if (r.loadPath) return r.loadPath;")
    lines.append("  return r.sourcePath;")
    lines.append("}")
    lines.append("")
    lines.append("export const HIDDEN_COMMANDS = [")
    lines.append('  "grill-me",')
    lines.append('  "wayfinder",')
    lines.append('  "to-tickets",')
    lines.append('  "to-spec",')
    lines.append("] as const;")
    lines.append("")
    lines.append(
        "/** Max chars to inline skill body into ROUTE_DIRECTIVE (else force path read). */"
    )
    lines.append("export const INLINE_BODY_MAX_CHARS = 16000;")
    lines.append("")
    lines.append("export function shouldInlineSkillBody(name: string): boolean {")
    lines.append("  const r = registryByName(name);")
    lines.append("  if (!r) return false;")
    lines.append("  const n = r.chars ?? 0;")
    lines.append("  return n > 0 && n <= INLINE_BODY_MAX_CHARS;")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def run_tests(pkg: Path) -> tuple[bool, str]:
    """Run the canonical package's Bun tests. Never the deployed copy."""
    if not (pkg / "decide-route.test.ts").exists():
        return True, "no tests present"
    try:
        proc = subprocess.run(
            ["bun", "test"],
            cwd=str(pkg),
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, "\n".join(out.splitlines()[-8:])
    except FileNotFoundError:
        return False, "bun not found"
    except subprocess.TimeoutExpired:
        return False, "bun test timed out"


def atomic_swap_dirs(a: Path, b: Path) -> None:
    """Atomically exchange two existing same-filesystem paths on macOS."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    ret = renameatx_np(
        -2,  # AT_FDCWD
        os.fsencode(a),
        -2,  # AT_FDCWD
        os.fsencode(b),
        RENAME_SWAP,
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), f"{a} <-> {b}")


def deploy(canonical: Path, live: Path) -> None:
    """Deploy canonical router package without a live-absent crash window.

    The new tree is staged as a sibling of live on the same filesystem. If live
    exists, macOS `renameatx_np(RENAME_SWAP)` atomically exchanges live and
    staging; live is never absent, even on process crash. After a successful
    swap, staging contains the old live tree and can be removed. If live is
    initially missing, a same-filesystem `os.rename(staging, live)` publishes it.
    """
    live.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".skill-router.staged.", dir=live.parent))
    try:
        shutil.copytree(canonical, staging, symlinks=True, dirs_exist_ok=True)
        if (live.exists() or live.is_symlink()) and sys.platform == "darwin":
            atomic_swap_dirs(live, staging)
        elif live.exists() or live.is_symlink():
            # No atomic exchange off macOS: move the old tree aside, then publish the new one.
            old = live.with_name(live.name + ".old")
            shutil.rmtree(old, ignore_errors=True)
            os.rename(live, old)
            os.rename(staging, live)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.rename(staging, live)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="regenerate canonical registry.ts, then deploy the whole package",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="compare canonical against live; mutate nothing",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="deprecated alias for --check",
    )
    ap.add_argument(
        "--test",
        action="store_true",
        help="run bun test in the canonical skill-router package",
    )
    ap.add_argument(
        "--allow-shrink",
        action="store_true",
        help="permit a write that drops a large fraction of existing rows",
    )
    ap.add_argument("--json-summary", type=Path, default=None)
    args = ap.parse_args()

    if args.dry_run:
        print(
            "[deprecated] --dry-run is an alias for --check",
            file=sys.stderr,
        )
    check_only = args.check or args.dry_run or not args.write

    canonical = canonical_pkg()
    live = live_pkg()
    canonical_registry = canonical / "registry.ts"

    if not canonical.is_dir():
        print(f"missing canonical package: {canonical}", file=sys.stderr)
        return 1

    rows = build_rows(scan())
    body = render_registry_ts(rows)

    summary: dict = {
        "mode": "check" if check_only else "write",
        "total": len(rows),
        "primary": sum(1 for r in rows if r["role"] == "primary"),
        "hidden": sum(1 for r in rows if r["role"] == "hidden-command"),
        "dead": sum(1 for r in rows if r["role"] == "dead"),
        "canonical": str(canonical),
        "live": str(live),
        "grill_me_load": next(
            (r["loadPath"] for r in rows if r["name"] == "grill-me"), None
        ),
        # --check never deploys; --write flips this only after a verified swap.
        "deployed": False,
    }
    if args.dry_run:
        summary["deprecated_alias"] = "--dry-run"

    def emit(code: int) -> int:
        if args.json_summary:
            args.json_summary.parent.mkdir(parents=True, exist_ok=True)
            args.json_summary.write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
        print(json.dumps(summary, indent=2))
        return code

    if check_only:
        stale = (
            not canonical_registry.exists()
            or canonical_registry.read_text(encoding="utf-8") != body
        )
        summary["registry_stale"] = stale
        below_floor = len(rows) < MIN_REGISTRY_ROWS
        summary["rows_below_floor"] = below_floor
        if below_floor:
            summary["drift"] = True
            summary["reason"] = (
                f"scanned {len(rows)} rows < floor {MIN_REGISTRY_ROWS}"
            )
            print(f"DRIFT: {summary['reason']}", file=sys.stderr)
            return emit(1)

        if not live.is_dir():
            summary["drift"] = True
            summary["reason"] = "live package missing"
            print(f"DRIFT: live package missing: {live}", file=sys.stderr)
            return emit(1)

        canonical_digest = tree_v1(canonical)
        live_digest = tree_v1(live)
        summary["canonical_tree_sha256"] = canonical_digest
        summary["deployed_tree_sha256"] = live_digest
        drift = stale or canonical_digest != live_digest
        summary["drift"] = drift
        if drift:
            summary["reason"] = (
                "canonical registry.ts stale" if stale else "live tree differs"
            )
            print(f"DRIFT: {summary['reason']}", file=sys.stderr)
            return emit(1)

        if args.test:
            test_ok, test_out = run_tests(canonical)
            summary["test_ok"] = test_ok
            summary["test_tail"] = test_out
            summary["test_root"] = str(canonical)
            print(test_out, file=sys.stderr)
            if not test_ok:
                return emit(1)
        return emit(0)

    if len(rows) < MIN_REGISTRY_ROWS:
        print(
            f"refusing to write registry.ts: scanned {len(rows)} rows < floor "
            f"{MIN_REGISTRY_ROWS}; skill trees look unavailable",
            file=sys.stderr,
        )
        summary["rows_below_floor"] = True
        return emit(3)

    prior_rows = 0
    if canonical_registry.exists():
        prior_rows = canonical_registry.read_text(encoding="utf-8").count("name: '")
    shrink = prior_rows - len(rows)
    summary["prior_rows"] = prior_rows
    summary["row_delta"] = -shrink if shrink else 0
    if (
        prior_rows
        and shrink > 0
        and shrink >= max(5, round(prior_rows * MAX_REGISTRY_SHRINK))
        and not args.allow_shrink
    ):
        print(
            f"refusing to write registry.ts: scan has {len(rows)} rows vs "
            f"{prior_rows} on disk ({shrink} fewer). A partial scan can clear "
            f"the absolute floor and still drop skills — check the scan roots "
            f"(ORCH_SKILLS_DIR). Pass --allow-shrink for a deliberate bulk "
            f"removal.",
            file=sys.stderr,
        )
        summary["shrink_blocked"] = True
        return emit(4)

    canonical_registry.parent.mkdir(parents=True, exist_ok=True)
    canonical_registry.write_text(body, encoding="utf-8")
    summary["registry_stale"] = False
    print(f"wrote {len(rows)} rows → {canonical_registry}", file=sys.stderr)

    if args.test:
        test_ok, test_out = run_tests(canonical)
        summary["test_ok"] = test_ok
        summary["test_tail"] = test_out
        summary["test_root"] = str(canonical)
        print(test_out, file=sys.stderr)
        if not test_ok:
            summary["deployed"] = False
            print("FAIL: canonical bun test; not deploying", file=sys.stderr)
            return emit(1)

    deploy(canonical, live)
    canonical_digest = tree_v1(canonical)
    live_digest = tree_v1(live)
    summary["canonical_tree_sha256"] = canonical_digest
    summary["deployed_tree_sha256"] = live_digest
    summary["deployed"] = True
    summary["drift"] = canonical_digest != live_digest
    if summary["drift"]:
        print("FAIL: deployed tree does not match canonical", file=sys.stderr)
        return emit(1)
    print(f"deployed canonical package → {live}", file=sys.stderr)
    return emit(0)


if __name__ == "__main__":
    raise SystemExit(main())
