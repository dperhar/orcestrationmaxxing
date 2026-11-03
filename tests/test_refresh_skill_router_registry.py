"""Contract tests for refresh-skill-router-registry.py.

Sources: approved plan section 1 + Contracts.

  - canonical source is `<repo>/omp/skill-router/`
  - live `$HOME/.omp/agent/extensions/skill-router/` is generated deploy output only
  - `--write` regenerates canonical registry.ts, then atomically deploys the
    COMPLETE canonical package
  - `--check` compares without mutating anything
  - `--test` runs bun tests from the CANONICAL package only
  - `--dry-run` is a deprecated alias for `--check`
  - directory hashes use the `tree-v1` contract

Roots are injected through ORCH_REPO_DIR/HOME (the script's existing
environment surface) so the approved CLI stays exactly
--write/--check/--test/--dry-run and the real live router is never touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "refresh-skill-router-registry.py"

CANONICAL_REL = Path("omp") / "skill-router"
LIVE_REL = Path(".omp") / "agent" / "extensions" / "skill-router"

PASSING_BUN_TEST = """import { expect, test } from "bun:test";
test("stub passes", () => {
  expect(1).toBe(1);
});
"""

FAILING_BUN_TEST = """import { expect, test } from "bun:test";
test("stub fails", () => {
  expect(1).toBe(2);
});
"""


# --- tree-v1: independent reimplementation of the Contracts wire format ---


def tree_v1(root: Path) -> str:
    entries: list[tuple[bytes, Path]] = []
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_file():
            entries.append((path.relative_to(root).as_posix().encode("utf-8"), path))
    entries.sort(key=lambda item: item[0])

    h = hashlib.sha256()
    h.update(b"OM-TREE\0v1\0")
    for rel, path in entries:
        if path.is_symlink():
            kind, data = b"L", os.readlink(path).encode("utf-8")
        else:
            kind, data = b"F", path.read_bytes()
        mode = os.lstat(path).st_mode & 0o777
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        h.update(kind)
        h.update(mode.to_bytes(4, "big"))
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


# --- fixtures ---


SEEDED_SKILL_COUNT = 200


def seed_skill_tree(home: Path, count: int = SEEDED_SKILL_COUNT) -> None:
    """Seed an available skill tree so scans clear the row-count floor.

     Tests that assert a successful write must
    represent that healthy state; an empty HOME is the degraded case the floor
    exists to refuse.
    """
    root = home / ".agents" / "skills"
    for i in range(count):
        name = f"seeded-skill-{i:03d}"
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: seeded fixture skill {i}\n---\n\nBody.\n",
            encoding="utf-8",
        )


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """(repo, home, canonical, live) — all inside tmp, paths contain spaces.

    HOME carries a seeded skill tree, so scans represent a healthy host.
    """
    repo = tmp_path / "repo with spaces"
    home = tmp_path / "home with spaces"
    canonical = repo / CANONICAL_REL
    live = home / LIVE_REL

    canonical.mkdir(parents=True)
    home.mkdir(parents=True)
    seed_skill_tree(home)

    (canonical / "index.ts").write_text("export const live = 1;\n", encoding="utf-8")
    (canonical / "types.ts").write_text("export type A = 1;\n", encoding="utf-8")
    (canonical / "registry.ts").write_text("// placeholder\n", encoding="utf-8")
    return repo, home, canonical, live


@pytest.fixture()
def degraded_env(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Same layout, but HOME has no skill tree at all: a degraded scan."""
    repo = tmp_path / "repo degraded"
    home = tmp_path / "home degraded"
    canonical = repo / CANONICAL_REL
    live = home / LIVE_REL

    canonical.mkdir(parents=True)
    home.mkdir(parents=True)

    (canonical / "index.ts").write_text("export const live = 1;\n", encoding="utf-8")
    (canonical / "types.ts").write_text("export type A = 1;\n", encoding="utf-8")
    return repo, home, canonical, live


def run(repo: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environ = os.environ.copy()
    environ["ORCH_REPO_DIR"] = str(repo)
    # Scan roots are a separate variable from the canonical package root; pin it
    # to the same temp tree so tests never scan the real a real skill install.
    environ["ORCH_SKILLS_DIR"] = str(repo)
    environ["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environ,
    )


def run_without_repo_env(cwd: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environ = os.environ.copy()
    environ.pop("ORCH_REPO_DIR", None)
    # This helper tests cwd-based canonical discovery; the scan root must still
    # be pinned so it never reaches the real a real skill install.
    environ["ORCH_SKILLS_DIR"] = str(cwd)
    environ["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=environ,
    )


def summary_of(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


# --- --write ---


def test_write_regenerates_canonical_registry_and_deploys_whole_package(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env
    proc = run(repo, home, "--write")
    assert proc.returncode == 0, proc.stderr

    # registry.ts is wholly generated, so the placeholder must be gone
    generated = (canonical / "registry.ts").read_text(encoding="utf-8")
    assert "// placeholder" not in generated

    # the COMPLETE package is deployed, not just registry.ts
    assert (live / "index.ts").exists()
    assert (live / "types.ts").exists()
    assert (live / "registry.ts").read_text(encoding="utf-8") == generated

    s = summary_of(proc)
    assert s["mode"] == "write"
    assert s["deployed"] is True
    assert s["drift"] is False
    assert s["canonical_tree_sha256"] == tree_v1(canonical)
    assert s["deployed_tree_sha256"] == tree_v1(live)
    assert s["canonical_tree_sha256"] == s["deployed_tree_sha256"]


def test_write_uses_current_worktree_root_when_repo_dir_is_unset(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env

    proc = run_without_repo_env(repo, home, "--write")
    assert proc.returncode == 0, proc.stderr

    assert (live / "index.ts").exists()
    assert summary_of(proc)["canonical"] == str(canonical)


def test_write_removes_live_files_absent_from_canonical(
    env: tuple[Path, Path, Path, Path],
) -> None:
    """Live is generated state: a stray live file must not survive a deploy."""
    repo, home, canonical, live = env
    live.mkdir(parents=True)
    stray = live / "stray-hand-edit.ts"
    stray.write_text("export const hand = true;\n", encoding="utf-8")

    assert run(repo, home, "--write").returncode == 0

    assert not stray.exists()
    assert tree_v1(canonical) == tree_v1(live)


def test_write_deploys_nested_private_skill_directories(
    env: tuple[Path, Path, Path, Path],
) -> None:
    """private-skills/example-gate/SKILL.md ships with the package."""
    repo, home, canonical, live = env
    nested = canonical / "private-skills" / "example-gate"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: example-gate\n---\n", encoding="utf-8")

    assert run(repo, home, "--write").returncode == 0

    deployed = live / "private-skills" / "example-gate" / "SKILL.md"
    assert deployed.read_text(encoding="utf-8").startswith("---")
    assert tree_v1(canonical) == tree_v1(live)


# --- --check ---


def test_check_reports_clean_after_write_and_mutates_nothing(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env
    assert run(repo, home, "--write").returncode == 0

    before_canonical = tree_v1(canonical)
    before_live = tree_v1(live)

    proc = run(repo, home, "--check")
    assert proc.returncode == 0, proc.stderr

    assert tree_v1(canonical) == before_canonical
    assert tree_v1(live) == before_live

    s = summary_of(proc)
    assert s["mode"] == "check"
    assert s["drift"] is False
    assert s["deployed"] is False


def test_check_detects_drift_without_repairing_it(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env
    assert run(repo, home, "--write").returncode == 0

    (live / "index.ts").write_text("export const live = 999;\n", encoding="utf-8")
    drifted = tree_v1(live)

    proc = run(repo, home, "--check")
    assert proc.returncode == 1
    assert summary_of(proc)["drift"] is True

    # check never repairs
    assert tree_v1(live) == drifted


def test_check_detects_stale_canonical_registry(
    env: tuple[Path, Path, Path, Path],
) -> None:
    """registry.ts is wholly generated, so a stale canonical copy is drift."""
    repo, home, canonical, live = env
    assert run(repo, home, "--write").returncode == 0

    (canonical / "registry.ts").write_text("// hand-edited\n", encoding="utf-8")

    proc = run(repo, home, "--check")
    assert proc.returncode == 1
    assert summary_of(proc)["drift"] is True
    # canonical is not silently regenerated by check
    assert (canonical / "registry.ts").read_text(encoding="utf-8") == "// hand-edited\n"


def test_check_reports_drift_when_live_package_is_missing(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env
    proc = run(repo, home, "--check")
    assert proc.returncode == 1
    assert summary_of(proc)["drift"] is True
    assert not live.exists()


# --- --dry-run alias ---


def test_dry_run_is_deprecated_alias_for_check(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env
    assert run(repo, home, "--write").returncode == 0
    before = tree_v1(live)

    proc = run(repo, home, "--dry-run")
    assert proc.returncode == 0, proc.stderr

    s = summary_of(proc)
    assert s["mode"] == "check"
    assert s["deprecated_alias"] == "--dry-run"
    assert tree_v1(live) == before


# --- --test ---


def test_test_flag_runs_bun_tests_from_canonical_package(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env
    (canonical / "decide-route.test.ts").write_text(PASSING_BUN_TEST, encoding="utf-8")

    proc = run(repo, home, "--write", "--test")
    assert proc.returncode == 0, proc.stderr

    s = summary_of(proc)
    assert s["test_ok"] is True
    assert s["test_root"] == str(canonical)
    assert str(live) != s["test_root"]


def test_failing_canonical_bun_test_fails_the_refresh(
    env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, live = env
    (canonical / "decide-route.test.ts").write_text(FAILING_BUN_TEST, encoding="utf-8")

    proc = run(repo, home, "--write", "--test")
    assert proc.returncode == 1
    assert summary_of(proc)["test_ok"] is False


# --- deploy durability ---


def load_refresher_module():
    spec = importlib.util.spec_from_file_location("refresher_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_atomic_swap_dirs_exchanges_trees_without_absent_live_window(
    tmp_path: Path,
) -> None:
    """Deploy uses a same-filesystem atomic directory swap on macOS.

    The primitive exchanges `live` and `staging` directly; it does not first
    rename live out of the way, so there is no crash point where live is absent.
    """
    mod = load_refresher_module()
    live = tmp_path / "live"
    staging = tmp_path / "staging"
    live.mkdir()
    staging.mkdir()
    (live / "old.txt").write_text("old\n", encoding="utf-8")
    (staging / "new.txt").write_text("new\n", encoding="utf-8")

    mod.atomic_swap_dirs(live, staging)

    assert (live / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert (staging / "old.txt").read_text(encoding="utf-8") == "old\n"


def test_deploy_failure_before_atomic_swap_leaves_previous_live_package(
    env: tuple[Path, Path, Path, Path],
) -> None:
    """Failures before the atomic swap must leave the live router untouched."""
    repo, home, canonical, live = env
    assert run(repo, home, "--write").returncode == 0
    good = tree_v1(live)

    mod = load_refresher_module()

    def failing_copytree(*_args, **_kwargs):
        raise OSError("injected staging failure")

    original = mod.shutil.copytree
    mod.shutil.copytree = failing_copytree
    try:
        with pytest.raises(OSError, match="injected staging failure"):
            mod.deploy(canonical, live)
    finally:
        mod.shutil.copytree = original

    assert live.is_dir()
    assert tree_v1(live) == good
    leftovers = [
        p.name
        for p in live.parent.iterdir()
        if p.name.startswith((".skill-router.staged.", ".skill-router.retired."))
    ]
    assert leftovers == []


# --- degraded-scan row-count floor ---


def test_write_refuses_to_truncate_registry_when_scan_is_degraded(
    degraded_env: tuple[Path, Path, Path, Path],
) -> None:
    """The armed regression: a degraded scan must never overwrite registry.ts.

    A pre-existing healthy registry.ts stays byte-identical and the run exits 3.
    """
    repo, home, canonical, _live = degraded_env
    healthy = "// healthy registry\n" + "x" * 60000
    registry = canonical / "registry.ts"
    registry.write_text(healthy, encoding="utf-8")

    proc = run(repo, home, "--write")

    assert proc.returncode == 3, proc.stderr
    assert registry.read_text(encoding="utf-8") == healthy
    assert "refusing to write registry.ts" in proc.stderr


def test_check_reports_rows_below_floor_when_scan_is_degraded(
    degraded_env: tuple[Path, Path, Path, Path],
) -> None:
    repo, home, canonical, _live = degraded_env
    (canonical / "registry.ts").write_text("// healthy\n", encoding="utf-8")

    proc = run(repo, home, "--check")

    s = summary_of(proc)
    assert s["rows_below_floor"] is True
    assert s["drift"] is True
    assert "rows <" in s["reason"]
    assert proc.returncode != 0


# --- relative shrink guard ---


def test_write_refuses_a_large_shrink_even_above_the_absolute_floor(
    env: tuple[Path, Path, Path, Path],
) -> None:
    """A partial scan that still clears the floor must not silently drop rows.

    Observed: pointing ORCH_REPO_DIR at a worktree missing the skill
    trees cut 226 rows to 186. That cleared the absolute floor, so the
    canonical registry lost 41 skills with no error. The removal below is sized
    to land ABOVE the floor, so only a relative guard can catch it.
    """
    repo, home, canonical, _live = env

    assert run(repo, home, "--write").returncode == 0
    healthy = (canonical / "registry.ts").read_text(encoding="utf-8")
    baseline_rows = healthy.count("name: '")

    # Same proportion as the real incident (~18% of rows), floor-clearing.
    drop = max(1, round(baseline_rows * 0.18))
    assert baseline_rows - drop > 20, (baseline_rows, drop)

    skills = sorted(
        p for p in (home / ".agents" / "skills").iterdir() if p.is_dir()
    )
    for skill_dir in skills[:drop]:
        shutil.rmtree(skill_dir)

    proc = run(repo, home, "--write")
    summary = summary_of(proc)

    assert summary["total"] > 20, "removal must stay above the absolute floor"
    assert proc.returncode != 0, proc.stdout
    assert (canonical / "registry.ts").read_text(encoding="utf-8") == healthy
    assert "shrink" in proc.stderr.lower() or "fewer" in proc.stderr.lower()


# --- deterministic winner among equal-rank duplicates ---


def test_scan_picks_the_same_duplicate_under_any_traversal_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal-rank duplicates must resolve deterministically.

    `~/gstack` holds nine copies of some skills at one providerRank, so a
    first-wins scan let filesystem order pick the winner: two runs produced
    byte-different registries and every `--check` reported a false "canonical
    registry.ts stale".

    Feeding permuted traversal orders is what makes this a regression test.
    Calling `scan()` repeatedly would reuse one real `rglob()` order and could
    pass against the old first-wins code.
    """
    mod = load_refresher_module()

    root = tmp_path / "gstack"
    hosts = (".hermes", ".cursor", ".opencode", ".agents")
    for host in hosts:
        d = root / host / "skills" / "dup-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: dup-skill\ndescription: from {host}\n---\n", encoding="utf-8"
        )
    monkeypatch.setattr(mod, "roots", lambda: [(root, 50, "gstack")])

    paths = [root / h / "skills" / "dup-skill" / "SKILL.md" for h in hosts]
    orders = [
        list(paths),
        list(reversed(paths)),
        [paths[1], paths[3], paths[0], paths[2]],
    ]

    winners = set()
    for order in orders:
        monkeypatch.setattr(
            mod.Path, "rglob", lambda self, pattern, _o=order: iter(_o)
        )
        winners.add(mod.scan()["dup-skill"]["path"])

    assert len(winners) == 1, f"winner depends on traversal order: {winners}"
    assert winners.pop().endswith(".agents/skills/dup-skill/SKILL.md")


def test_scan_ignores_backup_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.bak` sibling must never become the active source.

    `.` sorts before `/`, so a lexicographic tiebreak alone made
    `research/arxiv.bak/SKILL.md` beat `research/arxiv/SKILL.md`. The router
    reads `sourcePath`, so it would then serve the stale backup body forever.
    """
    mod = load_refresher_module()

    root = tmp_path / "skills"
    for name, body in (("arxiv", "active"), ("arxiv.bak", "stale")):
        d = root / "research" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: arxiv\ndescription: {body}\n---\n", encoding="utf-8"
        )
    monkeypatch.setattr(mod, "roots", lambda: [(root, 20, "agents")])

    winner = mod.scan()["arxiv"]["path"]

    assert ".bak" not in winner, f"backup directory won: {winner}"
    assert winner.endswith("research/arxiv/SKILL.md")


def test_scan_prefers_this_harness_host_over_path_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host choice must be explicit, not a side effect of path sorting.

    `~/gstack` ships one skill per agent host and the bodies differ — each
    hardcodes its own host paths. The preferred host must win even when another
    supported host sorts earlier, which a pure lexicographic tiebreak cannot do.
    """
    mod = load_refresher_module()

    root = tmp_path / "gstack"
    for host in (".agents", ".cursor", ".hermes"):
        d = root / host / "skills" / "dup-host"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: dup-host\ndescription: from {host}\n---\n", encoding="utf-8"
        )
    monkeypatch.setattr(mod, "roots", lambda: [(root, 50, "gstack")])

    # `.agents` also sorts first, so pin a later-sorting host to prove the
    # preference is doing the work rather than the path comparison.
    monkeypatch.setattr(mod, "PREFERRED_HOST_DIR", ".hermes")
    assert "/.hermes/" in mod.scan()["dup-host"]["path"]

    monkeypatch.setattr(mod, "PREFERRED_HOST_DIR", ".agents")
    assert "/.agents/" in mod.scan()["dup-host"]["path"]
