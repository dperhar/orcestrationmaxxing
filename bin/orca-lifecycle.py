#!/usr/bin/env python3
"""orca-lifecycle: lifecycle registry beside Orca orchestration.

Orca's own orchestration.db is the scheduler authority: Run, Task, Dispatch,
question, gate, and delivery. This registry never claims or schedules work on
its own. It wraps the Orca CLI calls of the triage loop (start-worker, wait) so
that each step is recorded, and it records what Orca does not: project identity,
lifecycle stage, review evidence, human approval, deduplicated transition events,
branch-preserving recovery verdicts, and durable context capture.

Task kinds: `code` ends in a git merge; `report` (research, writing, analysis)
ends in an accepted report file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

DEFAULT_DB = Path.home() / "Library/Application Support/orcestrationmaxxing/orca-lifecycle.db"
TRIAGE_CONFIG = Path.home() / "Library/Application Support/orcestrationmaxxing/orca-triage.json"
INSTALLED_LIFECYCLE = Path.home() / "Library/Scripts/orcestrationmaxxing/orca-lifecycle.py"
REPORTS_DIR = Path.home() / "Library/Application Support/orcestrationmaxxing/reports/orca-tasks"
# Every OMP worker starts with Orca's own OMP launch (`omp --extension <status> --config <fresh-session.yml>`)
# plus this overlay. The global config starts sessions in plan mode with an advisor; workers only execute.
# Compaction stays at the session defaults: re-reading context after an early compaction costs more.
NOPLAN_OVERLAY = Path(os.environ.get("ORCA_LIFECYCLE_OMP_OVERLAY")
                      or Path.home() / "Library/Application Support/orcestrationmaxxing/omp-worker-noplan.yml")
# The worker's own subagents (code-implementer, worker-advisor) get no runtime advisor either.
NOPLAN_OVERLAY_TEXT = ("plan:\n  defaultOnStartup: false\nadvisor:\n  enabled: false\n"
                       "task:\n  agentAdvisor:\n    code-implementer: \"off\"\n    worker-advisor: \"off\"\n")
OMP_STATUS_EXTENSION = Path.home() / ".omp/agent/extensions/orca-agent-status.ts"
OMP_FRESH_CONFIG = Path.home() / "Library/Application Support/orca/omp-managed-status-extension/fresh-session.yml"
SCHEMA_VERSION = 2
LEASE = dt.timedelta(minutes=15)
STALE_HEARTBEAT = dt.timedelta(minutes=30)
DEDUP_WINDOW = dt.timedelta(minutes=5)
# A Dispatch settles once. These signals dedupe forever, not only inside the window.
ONCE_PER_ENTITY = {"worker_done"}
REVIEW_KINDS = ("judge", "board")
DECISION_RE = re.compile(r"^\s*(?:[-*]\s*)?DECISION:\s*(.+?)\s*$", re.MULTILINE)
SOT_RE = re.compile(r"^\s*(?:[-*]\s*)?SOT\((architecture|business|dev|product)\):\s*(.+?)\s*$",
                    re.MULTILINE | re.IGNORECASE)
DECISIONS_LOG = "decisions.md"
SPEC_LIMIT = 12000

WORKER_CONTRACT = """
--- Lifecycle contract (added by orca-lifecycle.py) ---
You are a worker. The coordinator planned this ticket. Execute it. Do not enter plan mode, do not re-plan,
do not cut tickets.
- First, copy the exact worker_done command from your Orca preamble (`orca orchestration send ... --type
  worker_done ... --task-id ... --dispatch-id ... --outcome ...`) into your todo list, so it survives
  compaction. Send it with both --task-id and --dispatch-id.
Scope
- Do only this ticket. Adjacent fixes, other bugs, and "while I am here" changes are not done. Send `ask` to
  the coordinator: "out of scope: <what and why>". The coordinator decides: a small fix in this ticket, or a
  new ticket for a new worker.
- If your diff will be more than about 300 changed lines, send `ask` before you write it.
- Use the `ask` command from your Orca preamble for every question. Never open a local question prompt.
Subagents
- Write code through the `code-implementer` subagent (task tool, agent `code-implementer`): give it the exact files,
  the change, and the acceptance. Check its result against the acceptance yourself.
- Ask the `worker-advisor` subagent (task tool, agent `worker-advisor`) when the approach is unclear, after two
  failed attempts at the same problem, when you are unsure about scope, and once before worker_done. It is the
  only advisor in this session.
- To save cost, give cheap work to cheap subagents: `scout` (agent `scout`) finds and reads unknown code,
  `sonic` (agent `sonic`) does strictly mechanical edits and data collection. The live list with models is in
  the "Available Agents" section of your `task` tool.
Review and tests
- Do not review your own work: no review-gate skill, no reviewer subagents, no re-review. The coordinator
  reviews after worker_done.
- Code: TDD on the tests you add or change. Before worker_done, run once the repo's typecheck (for example
  `npx tsc --noEmit`) and the tests related to your changed files (for example
  `npx vitest related --run <changed files>`). Never run the full test suite more than once: tests that
  already fail on the base branch are not yours. The coordinator runs the full suite at verify and compares
  the failure names with the base branch.
- Keep your context small: read only the files you need, print only the tail of long outputs.
Delivery
- Code task: commit every change on your current branch. Do not push. Do not merge.
- Report task: write the result as Markdown to a file in your worktree. Pass its absolute path with
  `--report-path` in worker_done.
- Put each durable decision on its own line as `DECISION: <one short sentence>` in the worker_done body or the
  report. Mark nothing else as a decision.
- For each change to reality in architecture, business, dev process, or product, add a line
  `SOT(architecture|business|dev|product): <what is now true>`. Do not edit those SoT documents yourself:
  the triage agent updates them after your work is accepted.
- Send worker_done exactly once, with `--outcome succeeded` or `--outcome failed`. After worker_done, stop working.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id      TEXT PRIMARY KEY,
  git_remote_slug TEXT UNIQUE NOT NULL,
  name            TEXT,
  repo_path       TEXT NOT NULL,
  context_dir     TEXT NOT NULL,
  base_branch     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id             TEXT PRIMARY KEY,
  run_id              TEXT NOT NULL,
  project_id          TEXT NOT NULL REFERENCES projects(project_id),
  title               TEXT,
  kind                TEXT NOT NULL DEFAULT 'code' CHECK(kind IN ('code','report')),
  work_class          TEXT CHECK(work_class IN ('small','large')),
  acceptance_criteria TEXT NOT NULL,
  stage               TEXT NOT NULL DEFAULT 'routed' CHECK(stage IN (
    'routed','implementing','verifying','reviewing','rework',
    'awaiting_approval','approved','merged','closed','failed','abandoned')),
  merge_task_id       TEXT,
  merge_gate_id       TEXT,
  reviewed_sha        TEXT,
  updated_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  dispatch_id        TEXT PRIMARY KEY,
  task_id            TEXT NOT NULL REFERENCES tasks(task_id),
  harness_session_id TEXT,
  worktree_path      TEXT,
  branch             TEXT,
  heartbeat_at       TEXT,
  lease_expires_at   TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active','completed','failed','abandoned')),
  created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  review_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL REFERENCES tasks(task_id),
  kind       TEXT NOT NULL CHECK(kind IN ('verification','judge','board')),
  verdict    TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
  evidence   TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  task_id     TEXT PRIMARY KEY REFERENCES tasks(task_id),
  gate_id     TEXT NOT NULL,
  commit_sha  TEXT NOT NULL,
  resolved_by TEXT NOT NULL,
  answer      TEXT NOT NULL,
  resolved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
  task_id     TEXT NOT NULL REFERENCES tasks(task_id),
  sha256      TEXT NOT NULL,
  path        TEXT NOT NULL,
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (task_id, sha256)
);
CREATE TABLE IF NOT EXISTS tickets (
  ticket_path TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS sot_updates (
  task_id    TEXT NOT NULL REFERENCES tasks(task_id),
  area       TEXT NOT NULL,
  note       TEXT NOT NULL,
  files      TEXT,
  done_at    TEXT,
  PRIMARY KEY (task_id, area, note)
);
CREATE TABLE IF NOT EXISTS events (
  event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type   TEXT NOT NULL,
  entity_id     TEXT NOT NULL,
  transition    TEXT NOT NULL,
  evidence_json TEXT,
  fingerprint   TEXT NOT NULL,
  timestamp     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_key ON events(entity_type, entity_id, transition, event_id);
"""


class Refused(Exception):
    """The lifecycle state does not allow the requested action."""


class OrcaError(Exception):
    """An orca CLI call failed."""


# ---------------------------------------------------------------- time


_NOW: dt.datetime | None = None


def now() -> dt.datetime:
    return _NOW or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    text = text.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- storage


def connect() -> sqlite3.Connection:
    path = Path(os.environ.get("ORCA_LIFECYCLE_DB") or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    has_tables = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if has_tables and version == 1:
        # v2 adds tasks.work_class (small|large). Rows tracked before v2 keep NULL.
        db.execute("ALTER TABLE tasks ADD COLUMN work_class TEXT CHECK(work_class IN ('small','large'))")
        db.execute("PRAGMA user_version=2")
        version = 2
    if has_tables and version != SCHEMA_VERSION:
        raise SystemExit(f"registry {path} has schema version {version}, this tool needs {SCHEMA_VERSION};"
                         f" move the file aside (never delete it) and run again")
    db.executescript(SCHEMA)
    if not has_tables:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return db


def canonical(evidence: object) -> str:
    if evidence is None:
        return ""
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(entity_type: str, entity_id: str, transition: str, evidence_json: str) -> str:
    raw = "\x1f".join((entity_type, entity_id, transition, evidence_json))
    return hashlib.sha256(raw.encode()).hexdigest()


def log_event(db, entity_type: str, entity_id: str, transition: str, evidence=None) -> int:
    """Append one internal transition. The state machine guards against repeats."""
    ev = canonical(evidence)
    cur = db.execute(
        "INSERT INTO events (entity_type, entity_id, transition, evidence_json, fingerprint, timestamp)"
        " VALUES (?,?,?,?,?,?)",
        (entity_type, entity_id, transition, ev or None,
         fingerprint(entity_type, entity_id, transition, ev), iso(now())),
    )
    return cur.lastrowid


def append_event(db, entity_type: str, entity_id: str, transition: str, evidence=None) -> tuple[int, bool]:
    """Idempotent append for external signals. Returns (event_id, applied).

    Same key and same fingerprint: the signal is a duplicate; return the first event.
    Same key and other evidence: log a reconciliation event; do not apply it twice.
    The key window is DEDUP_WINDOW, or forever for ONCE_PER_ENTITY transitions.
    """
    ev = canonical(evidence)
    fp = fingerprint(entity_type, entity_id, transition, ev)
    sql = ("SELECT event_id, fingerprint FROM events"
           " WHERE entity_type=? AND entity_id=? AND transition=?")
    params: list = [entity_type, entity_id, transition]
    if transition not in ONCE_PER_ENTITY:
        sql += " AND timestamp >= ?"
        params.append(iso(now() - DEDUP_WINDOW))
    prior = db.execute(sql + " ORDER BY event_id LIMIT 1", params).fetchone()
    if prior is None:
        return log_event(db, entity_type, entity_id, transition, evidence), True
    if prior["fingerprint"] == fp:
        return prior["event_id"], False
    rid = log_event(db, "reconciliation", entity_id, "duplicate_event_mismatch", {
        "original_event_id": prior["event_id"], "entity_type": entity_type,
        "transition": transition, "new_evidence": evidence,
    })
    return rid, False


def get_task(db, task_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise Refused(f"task {task_id} is not tracked; run `track` first")
    return row


def get_project(db, project_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        raise Refused(f"project {project_id} is not registered; run `project-add` first")
    return row


def set_stage(db, task_id: str, new: str, allowed_from: tuple[str, ...], evidence=None) -> None:
    current = get_task(db, task_id)["stage"]
    if current not in allowed_from:
        raise Refused(f"task {task_id} is in stage '{current}'; '{new}' needs one of {list(allowed_from)}")
    db.execute("UPDATE tasks SET stage=?, updated_at=? WHERE task_id=?", (new, iso(now()), task_id))
    log_event(db, "task", task_id, f"stage:{new}", {"from": current, **(evidence or {})})
    if STAGE_BOARD.get(new) and STAGE_BOARD.get(new) != STAGE_BOARD.get(current):
        attempt = latest_attempt(db, task_id)
        if attempt is not None and attempt["worktree_path"]:
            set_board_status(db, attempt["dispatch_id"], attempt["worktree_path"], board_status_id(STAGE_BOARD[new]))


def latest_attempt(db, task_id: str, status: str | None = None) -> sqlite3.Row | None:
    sql, params = "SELECT * FROM attempts WHERE task_id=?", [task_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    return db.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 1", params).fetchone()


# ---------------------------------------------------------------- external tools


def orca_raw(*args: str, timeout: float = 120) -> tuple[bool, dict]:
    """Run one orca CLI call. Returns (ok, parsed JSON). Raises OrcaError only when there is no JSON."""
    exe = shlex.split(os.environ.get("ORCA_CLI_COMMAND") or "orca")
    try:
        proc = subprocess.run([*exe, *args, "--json"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OrcaError(f"orca {' '.join(args[:2])}: {exc}") from exc
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise OrcaError(f"orca {' '.join(args[:2])}: exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.returncode == 0 and bool(data.get("ok", False)), data


def orca(*args: str, timeout: float = 120) -> dict:
    ok, data = orca_raw(*args, timeout=timeout)
    if not ok:
        raise OrcaError(f"orca {' '.join(args[:2])}: {json.dumps(data.get('error', data))[:300]}")
    return data.get("result") or {}


def git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise Refused(f"git {' '.join(args)} in {repo}: {proc.stderr.strip()[:300]}")
    return proc


def remote_slug(repo: str) -> str | None:
    remotes = git(repo, "remote", check=False).stdout.split()
    if not remotes:
        return None
    name = "origin" if "origin" in remotes else remotes[0]
    url = git(repo, "remote", "get-url", name, check=False).stdout.strip()
    parts = [p for p in re.split(r"[/:]", url.removesuffix(".git")) if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def pr_exists(repo: str, branch: str) -> bool | None:
    """True or False from `gh`; None when gh cannot answer for this repo."""
    if not shutil.which("gh") or "github.com" not in git(repo, "remote", "-v", check=False).stdout:
        return None
    try:
        proc = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number"],
                              cwd=repo, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return bool(json.loads(proc.stdout or "[]"))


def orca_user_data() -> Path:
    return Path(os.environ.get("ORCA_USER_DATA_PATH") or Path.home() / "Library/Application Support/orca")


def orca_readonly_db() -> sqlite3.Connection | None:
    path = Path(os.environ.get("ORCA_ORCHESTRATION_DB") or orca_user_data() / "orchestration.db")
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


# Orca board column for each stage. "fix" is the user's "To Fix" column when the board has one.
STAGE_BOARD = {
    "implementing": "in-progress",
    "verifying": "in-review", "reviewing": "in-review", "awaiting_approval": "in-review", "approved": "in-review",
    "rework": "fix", "abandoned": "fix",
    "merged": "completed", "closed": "completed",
}


def board_status_id(column: str) -> str:
    """Board status id. Orca has no CLI that lists custom columns, so read its settings file read-only."""
    if column != "fix":
        return column
    try:
        statuses = json.loads((orca_user_data() / "orca-data.json").read_text())["ui"]["workspaceStatuses"]
    except (OSError, ValueError, KeyError, TypeError):
        return "in-progress"
    return next((s["id"] for s in statuses if str(s.get("label", "")).strip().lower() == "to fix"), "in-progress")


def set_board_status(db, dispatch_id: str, worktree_path: str, status: str) -> None:
    """Move the worker worktree on the Orca board. A board failure never blocks the lifecycle; it is logged."""
    try:
        # Short timeout: this call runs inside the registry transaction and is not essential.
        ok, data = orca_raw("worktree", "set", "--worktree", f"path:{worktree_path}", "--workspace-status", status,
                            timeout=15)
        error = None if ok else (data.get("error") or "orca returned ok=false")
    except OrcaError as exc:
        error = str(exc)
    log_event(db, "attempt", dispatch_id, "board_status_failed" if error else "board_status",
              {"status": status, "worktree": worktree_path, **({"error": error} if error else {})})


def pending_questions(run_ids: list[str]) -> list[dict]:
    """Orca has no CLI verb that lists pending questions, so read question_threads read-only."""
    odb = orca_readonly_db()
    if odb is None or not run_ids:
        return []
    marks = ",".join("?" * len(run_ids))
    rows = odb.execute(
        "SELECT q.message_id, q.run_id, q.dispatch_id, d.task_id, m.body, m.payload, q.created_at"
        " FROM question_threads q"
        " LEFT JOIN dispatch_contexts d ON d.id = q.dispatch_id"
        " LEFT JOIN messages m ON m.id = q.message_id"
        f" WHERE q.status = 'pending' AND q.run_id IN ({marks}) ORDER BY q.created_at",
        run_ids,
    ).fetchall()
    odb.close()
    return [dict(r) for r in rows]


def question_thread(message_id: str) -> dict | None:
    odb = orca_readonly_db()
    if odb is None:
        return None
    row = odb.execute(
        "SELECT q.message_id, q.run_id, q.dispatch_id, d.task_id, q.status FROM question_threads q"
        " LEFT JOIN dispatch_contexts d ON d.id = q.dispatch_id WHERE q.message_id = ?",
        (message_id,),
    ).fetchone()
    odb.close()
    return dict(row) if row else None


def orca_worker_done(dispatch_id: str) -> dict | None:
    """The first worker_done message Orca stored for a Dispatch (read-only)."""
    odb = orca_readonly_db()
    if odb is None:
        return None
    row = odb.execute(
        "SELECT id, body, payload FROM messages WHERE type = 'worker_done'"
        " AND json_valid(payload) AND json_extract(payload, '$.dispatchId') = ? ORDER BY sequence LIMIT 1",
        (dispatch_id,),
    ).fetchone()
    odb.close()
    return dict(row) if row else None


def orca_workers(run_id: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    cursor = None
    while True:
        args = ["orchestration", "worker-list", "--run", run_id]
        if cursor:
            args += ["--cursor", cursor]
        result = orca(*args)
        for w in result.get("workers", []):
            rows[w["dispatchId"]] = w
        cursor = (result.get("page") or {}).get("nextCursor")
        if not cursor:
            return rows


# ---------------------------------------------------------------- commands


def default_base_branch(repo: str) -> str:
    head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).stdout.strip()
    if head:
        return head.split("/", 1)[1]
    for name in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0:
            return name
    raise Refused(f"{repo} has no origin/HEAD, main, or master; pass --base-branch")


def cmd_project_add(db, a) -> dict:
    repo = str(Path(a.repo_path).resolve())
    git(repo, "rev-parse", "--git-dir")
    slug = a.slug or remote_slug(repo)
    if not slug:
        raise Refused(f"{repo} has no git remote; pass --slug owner/repo")
    context_dir = str(Path(a.context_dir).resolve()) if a.context_dir else repo
    project_id = a.project_id or slug
    base = a.base_branch or default_base_branch(repo)
    git(repo, "rev-parse", "--verify", f"refs/heads/{base}")
    db.execute(
        "INSERT INTO projects (project_id, git_remote_slug, name, repo_path, context_dir, base_branch)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET git_remote_slug=excluded.git_remote_slug,"
        " name=excluded.name, repo_path=excluded.repo_path, context_dir=excluded.context_dir,"
        " base_branch=excluded.base_branch",
        (project_id, slug, a.name or slug, repo, context_dir, base),
    )
    return {"project_id": project_id, "git_remote_slug": slug, "repo_path": repo, "context_dir": context_dir,
            "base_branch": base}


def track_task(db, task_id: str, run_id: str, project_id: str, acceptance: str, kind: str,
               title: str | None, work_class: str | None = None) -> None:
    get_project(db, project_id)
    db.execute(
        "INSERT INTO tasks (task_id, run_id, project_id, title, kind, work_class, acceptance_criteria, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (task_id, run_id, project_id, title, kind, work_class, acceptance, iso(now())),
    )
    log_event(db, "task", task_id, "stage:routed",
              {"run_id": run_id, "project_id": project_id, "kind": kind, "class": work_class})


def cmd_track(db, a) -> dict:
    get_project(db, a.project)
    tasks = orca("orchestration", "task-list", "--run", a.run).get("tasks", [])
    match = next((t for t in tasks if t.get("id") == a.task), None)
    if match is None:
        raise Refused(f"Orca has no task {a.task} in run {a.run}")
    track_task(db, a.task, a.run, a.project, a.acceptance, a.kind, a.title or match.get("task_title"))
    return {"task_id": a.task, "stage": "routed", "kind": a.kind}


def record_attempt(db, task_id: str, dispatch_id: str, worktree_path: str | None, branch: str | None,
                   session: str | None, work_class: str | None = None) -> dict:
    task = get_task(db, task_id)
    active = latest_attempt(db, task_id, "active")
    if active is not None:
        raise Refused(f"task {task_id} already has active Dispatch {active['dispatch_id']};"
                      " settle it, or let `recover` abandon it, before you record a new one")
    repo = get_project(db, task["project_id"])["repo_path"]
    is_git = bool(worktree_path) and bool(_git_common_dir(worktree_path))
    if task["kind"] == "code" or is_git:
        if worktree_path:
            if _git_common_dir(worktree_path) != _git_common_dir(repo):
                raise Refused(f"{worktree_path} is not a worktree of the project repo {repo};"
                              f" start the worker with `--repo {orca_repo_selector(repo)}`")
            if not branch:
                branch = git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                if branch == "HEAD":
                    raise Refused(f"{worktree_path} has a detached HEAD; the worker must commit on a branch")
        if branch and git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode:
            raise Refused(f"branch '{branch}' is not in the project repo {repo};"
                          f" start the worker with `--repo {orca_repo_selector(repo)}`")
    db.execute(
        "INSERT INTO attempts (dispatch_id, task_id, harness_session_id, worktree_path, branch,"
        " heartbeat_at, lease_expires_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (dispatch_id, task_id, session, worktree_path, branch, iso(now()), iso(now() + LEASE), iso(now())),
    )
    started = {"task_id": task_id, "branch": branch}
    if work_class:
        started["class"] = work_class
    log_event(db, "attempt", dispatch_id, "started", started)
    if task["stage"] != "implementing":
        set_stage(db, task_id, "implementing", ("routed", "rework", "abandoned", "failed"),
                  {"dispatch_id": dispatch_id})
    return {"dispatch_id": dispatch_id, "task_id": task_id, "worktree_path": worktree_path, "branch": branch,
            "stage": "implementing"}


def cmd_attempt(db, a) -> dict:
    return record_attempt(db, a.task, a.dispatch, a.worktree_path, a.branch, a.session)


SETTLEABLE = ("implementing", "routed", "abandoned")


def _apply_worker_done(db, task_id: str, dispatch_id: str, outcome: str) -> str:
    db.execute("UPDATE attempts SET status=? WHERE dispatch_id=?",
               ("completed" if outcome == "succeeded" else "failed", dispatch_id))
    if outcome == "succeeded":
        log_event(db, "task", task_id, "task_completed", {"dispatch_id": dispatch_id})
        set_stage(db, task_id, "verifying", SETTLEABLE, {"dispatch_id": dispatch_id})
        return "verifying"
    set_stage(db, task_id, "rework", SETTLEABLE, {"dispatch_id": dispatch_id})
    return "rework"


def cmd_ingest(db, a) -> dict:
    raw = Path(a.file).read_text() if a.file and a.file != "-" else sys.stdin.read()
    data = json.loads(raw)
    return ingest_data(db, data.get("result", data) if isinstance(data, dict) else {})


def ingest_data(db, result: dict) -> dict:
    report: dict = {"processed": [], "duplicates": 0, "superseded": 0, "ignored": 0, "discarded": [], "errors": []}
    discarded = {r["entity_id"] for r in db.execute(
        "SELECT entity_id FROM events WHERE entity_type='message' AND transition='discarded'")}
    for m in result.get("messages", []):
        if m.get("id") in discarded:
            # The coordinator discarded this message with a recorded reason (`discard`), so it no longer blocks
            # the ack of its batch.
            report["discarded"].append(m.get("id"))
            continue
        payload = m.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if not isinstance(payload, dict):
            report["errors"].append({"message_id": m.get("id"), "error": "payload is not a JSON object"})
            continue
        kind, task_id, dispatch_id = m.get("type"), payload.get("taskId"), payload.get("dispatchId")
        if kind == "worker_done":
            if not dispatch_id:
                report["errors"].append({"message_id": m.get("id"), "error": "worker_done without a dispatchId"})
                continue
            outcome = payload.get("outcome")
            if outcome not in ("succeeded", "failed"):
                report["errors"].append({"message_id": m.get("id"), "error": "worker_done without a valid outcome"})
                continue
            row = db.execute("SELECT a.task_id, t.stage FROM attempts a JOIN tasks t USING(task_id)"
                             " WHERE a.dispatch_id=?", (dispatch_id,)).fetchone()
            if row is not None and not task_id:
                # Some workers omit --task-id; the recorded attempt names the task of its Dispatch.
                task_id = row["task_id"]
            if row is None or row["task_id"] != task_id:
                report["errors"].append({"message_id": m.get("id"), "error":
                    f"no recorded attempt links dispatch {dispatch_id} to task {task_id};"
                    " run `track` and `attempt`, then ingest this batch again"})
                continue
            evidence = {"task_id": task_id, "payload": payload, "body": m.get("body", "")}
            if latest_attempt(db, task_id)["dispatch_id"] != dispatch_id:
                # A retry replaced this attempt. Keep the fact; only the current attempt can settle the task.
                append_event(db, "attempt", dispatch_id, "worker_done_superseded", evidence)
                report["superseded"] += 1
                continue
            # One completion per Dispatch: a repeat with any other outcome or body is reconciled, never applied.
            seen = db.execute("SELECT 1 FROM events WHERE entity_type='attempt' AND entity_id=?"
                              " AND transition='worker_done'", (dispatch_id,)).fetchone()
            if not seen and row["stage"] in ("failed", "closed"):
                # The task already ended. Keep the report; a late completion cannot reopen it.
                append_event(db, "attempt", dispatch_id, "worker_done_late", {**evidence, "stage": row["stage"]})
                report["superseded"] += 1
                continue
            if not seen and row["stage"] not in SETTLEABLE:
                # Not recorded, so the same message applies once the stage is corrected and the batch replays.
                report["errors"].append({"message_id": m.get("id"), "error":
                    f"task {task_id} is in stage '{row['stage']}'; worker_done not applied"})
                continue
            _, applied = append_event(db, "attempt", dispatch_id, "worker_done", evidence)
            if not applied:
                report["duplicates"] += 1
            else:
                stage = _apply_worker_done(db, task_id, dispatch_id, outcome)
                report["processed"].append({"message_id": m.get("id"), "task_id": task_id, "stage": stage})
        elif kind == "heartbeat" and dispatch_id:
            cur = db.execute(
                "UPDATE attempts SET heartbeat_at=?, lease_expires_at=? WHERE dispatch_id=? AND status='active'",
                (iso(now()), iso(now() + LEASE), dispatch_id))
            if cur.rowcount:
                report["processed"].append({"message_id": m.get("id"), "dispatch_id": dispatch_id,
                                            "lease_extended": True})
            else:
                report["ignored"] += 1
        elif kind in ("question", "escalation"):
            if kind == "question":
                entity_type, entity_id, transition = "question", m["id"], "asked"
            else:
                entity_type, entity_id, transition = "attempt", dispatch_id or m["id"], "escalation"
            _, applied = append_event(db, entity_type, entity_id, transition,
                                      {"task_id": task_id, "dispatch_id": dispatch_id,
                                       "subject": m.get("subject"), "body": m.get("body", "")})
            if applied:
                report["processed"].append({"message_id": m.get("id"), "type": kind, "task_id": task_id,
                                            "dispatch_id": dispatch_id, "body": (m.get("body") or "")[:500]})
            else:
                report["duplicates"] += 1
        else:
            report["ignored"] += 1
    delivery = result.get("delivery") or {}
    delivery_id = delivery.get("id") if isinstance(delivery, dict) else None
    delivery_id = delivery_id or result.get("deliveryId")
    if delivery_id and report["errors"]:
        report["ack_withheld"] = f"{len(report['errors'])} message(s) not processed; inspect before ack {delivery_id}"
    elif delivery_id:
        report["ack"] = ["orca", "orchestration", "check", "--ack", delivery_id]
    return report


def _unlocked(db, fn):
    """Commit, run `fn` without holding the registry lock, then reopen the transaction main() expects."""
    db.execute("COMMIT")
    try:
        return fn()
    finally:
        db.execute("BEGIN IMMEDIATE")


def cmd_wait(db, a) -> dict:
    """One wake cycle: wait for Orca mail, record every batch, ack it only after it is recorded."""
    run = ["--run", a.run] if a.run else []
    wait_args = ["orchestration", "check", "--wait", "--types", "worker_done,escalation,question",
                 "--timeout-ms", str(a.timeout_ms), *run]
    result = _unlocked(db, lambda: orca(*wait_args, timeout=a.timeout_ms / 1000 + 60))
    out: dict = {"batches": 0, "processed": [], "duplicates": 0, "superseded": 0, "errors": [], "questions": []}
    while result.get("messages"):
        report = ingest_data(db, result)
        out["batches"] += 1
        out["processed"] += report["processed"]
        for key in ("duplicates", "superseded"):
            out[key] += report[key]
        if report["errors"]:
            out["errors"] += report["errors"]
            out["ack_withheld"] = report.get("ack_withheld")
            break
        if "ack" not in report:
            break
        ack_args = ["orchestration", "check", "--ack", report["ack"][-1], *run]
        result = _unlocked(db, lambda: orca(*ack_args))  # commits the batch before the ack
    if not out["batches"]:
        out["timed_out"] = True
    for p in out["processed"]:
        if p.get("type") == "question":
            thread = question_thread(p["message_id"]) or {}
            out["questions"].append({"message_id": p["message_id"], "task_id": thread.get("task_id"),
                                     "body": p["body"],
                                     "next": f"orca-lifecycle.py answer --question {p['message_id']} --body \"<answer>\""})
    if out["errors"]:
        out["exit_code"] = 1
        out["next"] = "fix each error (for example `track` + `attempt` for an unknown Dispatch), then run `wait` again"
    return out


def _branch_tip(db, task_id: str) -> tuple[sqlite3.Row, str, str]:
    """The completed attempt, its repo, and the current commit of its branch."""
    attempt = latest_attempt(db, task_id, "completed")
    if attempt is None or not attempt["branch"]:
        raise Refused(f"task {task_id} has no completed attempt with a branch")
    repo = get_project(db, get_task(db, task_id)["project_id"])["repo_path"]
    tip = git(repo, "rev-parse", "--verify", f"refs/heads/{attempt['branch']}^{{commit}}").stdout.strip()
    return attempt, repo, tip


def _worker_done_evidence(db, dispatch_id: str) -> dict:
    row = db.execute("SELECT evidence_json FROM events WHERE entity_type='attempt' AND entity_id=?"
                     " AND transition='worker_done' ORDER BY event_id LIMIT 1", (dispatch_id,)).fetchone()
    return json.loads(row["evidence_json"]) if row else {}


def _pin_report(db, task: sqlite3.Row) -> str:
    """Store the report the worker named in worker_done; reviews and approval refer to its hash."""
    attempt = latest_attempt(db, task["task_id"], "completed")
    path = (_worker_done_evidence(db, attempt["dispatch_id"]).get("payload") or {}).get("reportPath") \
        if attempt else None
    if not path or not Path(path).is_file():
        raise Refused(f"task {task['task_id']} is a report task, but worker_done named no readable report"
                      f" ({path!r}); the worker must pass --report-path")
    content = Path(path).read_text(encoding="utf-8")
    sha = hashlib.sha256(content.encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO reports (task_id, sha256, path, content, created_at) VALUES (?,?,?,?,?)",
               (task["task_id"], sha, path, content, iso(now())))
    return sha


def _current_pin(db, task: sqlite3.Row) -> str:
    """What the reviews and the approval refer to: the branch tip (code) or the stored report hash (report)."""
    if task["kind"] == "report":
        return task["reviewed_sha"]  # the stored report never changes; a new report needs a new attempt
    return _branch_tip(db, task["task_id"])[2]


def cmd_verify(db, a) -> dict:
    task = get_task(db, a.task)
    if task["stage"] != "verifying":
        raise Refused(f"task {a.task} is in stage '{task['stage']}'; verify needs 'verifying'")
    db.execute("INSERT INTO reviews (task_id, kind, verdict, evidence, created_at) VALUES (?,?,?,?,?)",
               (a.task, "verification", a.verdict, a.evidence, iso(now())))
    if a.verdict == "fail":
        set_stage(db, a.task, "rework", ("verifying",), {"verdict": a.verdict})
        return {"task_id": a.task, "stage": "rework", "next": _rework_hint(task)}
    # Every later verdict and the human approval refer to this exact commit or report.
    pin = _pin_report(db, task) if task["kind"] == "report" else _branch_tip(db, a.task)[2]
    db.execute("UPDATE tasks SET reviewed_sha=? WHERE task_id=?", (pin, a.task))
    set_stage(db, a.task, "reviewing", ("verifying",), {"verdict": a.verdict, "reviewed_sha": pin})
    target = f"report {pin[:12]}" if task["kind"] == "report" else f"commit {pin[:12]}"
    return {"task_id": a.task, "stage": "reviewing", "reviewed_sha": pin,
            "next": f"run judge (reviewer) and board (council-strategist) on {target},"
                    f" then `review --task {a.task}`"}


def _git_common_dir(path: str) -> str:
    return git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()


def orca_repo_path(repo_path: str) -> str:
    """Orca resolves `--repo path:` only for the main checkout, not for a linked worktree."""
    first = git(repo_path, "worktree", "list", "--porcelain", check=False).stdout.split("\n", 1)[0]
    main = first.removeprefix("worktree ") if first.startswith("worktree ") else repo_path
    return f"path:{main}"


def orca_repo_selector(repo_path: str) -> str:
    """The `--repo` value, shell-quoted for display in a hint."""
    return shlex.quote(orca_repo_path(repo_path))


def _retry_hint(task: sqlite3.Row) -> str:
    # start-worker reads the Orca Task status and picks reopen, --retry-of, or a plain start itself.
    return (f"`orca-lifecycle.py start-worker --task {task['task_id']}"
            f" --class {task['work_class'] or '<small|large>'} --agent <agent>`")


def _rework_hint(task: sqlite3.Row) -> str:
    return f"run the `improve` skill on the failed evidence for {task['task_id']}, then {_retry_hint(task)}"


def _worktree_name(db, task: sqlite3.Row) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (task["title"] or "task").lower()).strip("-")[:32] or "task"
    n = db.execute("SELECT count(*) FROM attempts WHERE task_id=?", (task["task_id"],)).fetchone()[0] + 1
    return f"{slug}-{task['task_id'][-6:]}-a{n}"


def cmd_start_worker(db, a) -> dict:
    """Create (optional), track, and start one worker, then record its Dispatch. One command, no gaps."""
    if a.spec:
        if a.task or not (a.project and a.run and a.acceptance and a.title):
            raise Refused("--spec needs --project, --run, --title, and --acceptance, and no --task")
        _require_chat_in_repo(get_project(db, a.project)["repo_path"])
        spec = f"{a.spec.rstrip()}\n{WORKER_CONTRACT}"
        if len(spec) > SPEC_LIMIT:
            raise Refused(f"spec is {len(spec)} characters; the limit is {SPEC_LIMIT}. Point to a file instead")
        create = ["orchestration", "task-create", "--run", a.run, "--task-title", a.title[:120], "--spec", spec]
        if a.deps:
            create += ["--deps", a.deps]
        task_id = orca(*create)["task"]["id"]
        track_task(db, task_id, a.run, a.project, a.acceptance, a.kind, a.title, a.work_class)
    elif a.task:
        task_id = a.task
        if db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
            if not (a.project and a.run and a.acceptance):
                raise Refused(f"task {task_id} is not tracked; pass --project, --run, and --acceptance")
            listed = orca("orchestration", "task-list", "--run", a.run).get("tasks", [])
            match = next((t for t in listed if t.get("id") == task_id), None)
            if match is None:
                raise Refused(f"Orca has no task {task_id} in run {a.run}")
            track_task(db, task_id, a.run, a.project, a.acceptance, a.kind, a.title or match.get("task_title"),
                       a.work_class)
    else:
        raise Refused("pass --task <id> or --spec <text>")
    task = get_task(db, task_id)
    if task["work_class"] == "large" and a.work_class == "small":
        raise Refused(f"task {task_id} is tracked as large (imported ticket); start it with --class large")

    if latest_attempt(db, task_id, "active") is not None:
        raise Refused(f"task {task_id} already has an active Dispatch; settle it or let `recover` abandon it")
    if task["work_class"] != a.work_class:
        db.execute("UPDATE tasks SET work_class=? WHERE task_id=?", (a.work_class, task_id))
    project = get_project(db, task["project_id"])
    _require_chat_in_repo(project["repo_path"])
    status = next((t.get("status") for t in orca("orchestration", "task-list", "--run", task["run_id"])
                   .get("tasks", []) if t.get("id") == task_id), None)
    name = a.name or _worktree_name(db, task)
    base = a.base_branch or project["base_branch"]
    args = ["orchestration", "worker-start", "--task", task_id, "--run", task["run_id"],
            "--repo", orca_repo_path(project["repo_path"]), "--worktree", "new-child",
            "--name", name, "--base-branch", base, "--agent", a.agent, "--timeout-ms", "300000"]
    if a.model:
        args += ["--model", a.model]
    if a.setup:
        args += ["--setup", a.setup]
    retry: list[str] = []
    # Orca 1.4.222 rules (orcad.js): --retry-of needs Task failed/blocked; a plain start needs Task ready.
    if status == "completed":
        orca("orchestration", "task-update", "--id", task_id, "--run", task["run_id"], "--status", "ready",
             "--result", "reopened for rework by orca-lifecycle.py")
        log_event(db, "task", task_id, "orca_task_reopened", {"from": status})
    elif status in ("failed", "blocked"):
        last = latest_attempt(db, task_id)
        if last is None:
            raise Refused(f"Orca Task {task_id} is {status} and has no recorded Dispatch to retry")
        retry = ["--retry-of", last["dispatch_id"]]
    elif status != "ready":
        raise Refused(f"Orca Task {task_id} is '{status}'; only ready, completed, failed, or blocked can start")
    if a.agent == "omp":
        ok, data, large_worktree = _unlocked(db, lambda: _start_large(task, project, a, name, base, retry))
    else:
        ok, data = _unlocked(db, lambda: orca_raw(*args, *retry, timeout=360))
        large_worktree = None
    result = data.get("result") or {}
    if not ok:
        log_event(db, "task", task_id, "worker_start_failed",
                  {"error": data.get("error"), "failedStage": result.get("failedStage")})
        return {"task_id": task_id, "error": data.get("error"), "failedStage": result.get("failedStage"),
                "residualResources": result.get("residualResources"), "exit_code": 1,
                "next": "do not relaunch; read failedStage and residualResources, then"
                        " `orca skills get orchestration --reference references/recovery-and-cleanup.md`"}
    worktree = large_worktree or next((e["id"].split("::", 1)[1] for e in result.get("effects", [])
                                       if e.get("kind") == "worktree" and "::" in str(e.get("id"))), None)
    out = record_attempt(db, task_id, result["dispatchId"], worktree, None, a.session, a.work_class)
    if result.get("briefSubmitted"):
        log_event(db, "attempt", result["dispatchId"], "brief_submitted", {"reason": "brief left in input box"})
    out["next"] = "orca-lifecycle.py wait"
    return out


def _start_large(task: sqlite3.Row, project: sqlite3.Row, a, name: str, base: str,
                 retry: list[str]) -> tuple[bool, dict, str | None]:
    """Start an OMP worker with the worker overlay and keep its Dispatch: Orca's worker-start has no argv or env
    hook. worktree create, then terminal create running Orca's own OMP command plus the overlay, then
    worker-start --terminal adopts it once the TUI is idle."""
    if not NOPLAN_OVERLAY.exists() or NOPLAN_OVERLAY.read_text() != NOPLAN_OVERLAY_TEXT:
        NOPLAN_OVERLAY.parent.mkdir(parents=True, exist_ok=True)
        NOPLAN_OVERLAY.write_text(NOPLAN_OVERLAY_TEXT)
    command = ["omp", "--extension", os.environ.get("ORCA_OMP_STATUS_EXTENSION") or str(OMP_STATUS_EXTENSION),
               "--config", os.environ.get("ORCA_OMP_FRESH_CONFIG") or str(OMP_FRESH_CONFIG),
               "--config", str(NOPLAN_OVERLAY)]
    if a.model:
        command += ["--model", a.model]
    residual: list[str] = []

    def failed(stage: str, data: dict) -> tuple[bool, dict, None]:
        return False, {"error": data.get("error"),
                       "result": {"failedStage": stage, "residualResources": residual}}, None

    create = ["worktree", "create", "--name", name, "--repo", orca_repo_path(project["repo_path"]),
              "--base-branch", base]
    if a.setup:
        create += ["--setup", a.setup]
    ok, data = orca_raw(*create)
    if not ok:
        return failed("worktree-create", data)
    wid = data["result"]["worktree"]["id"]
    residual.append(wid)
    ok, data = orca_raw("terminal", "create", "--worktree", f"id:{wid}", "--title", name,
                        "--command", shlex.join(command))
    if not ok:
        return failed("terminal-create", data)
    handle = data["result"]["terminal"]["handle"]
    residual.append(handle)
    for stage, call in (("tui-idle", ("terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                                      "--timeout-ms", "90000")),):
        ok, data = orca_raw(*call, timeout=150)
        if not ok:
            return failed(stage, data)
    ok, data = orca_raw("orchestration", "worker-start", "--task", task["task_id"], "--run", task["run_id"],
                        "--worktree", f"id:{wid}", "--terminal", handle, "--timeout-ms", "300000", *retry,
                        timeout=360)
    if not ok:
        result = data.get("result") or {}
        return False, {"error": data.get("error"), "result": {
            "failedStage": f"worker-start:{result.get('failedStage')}",
            "residualResources": residual + list(result.get("residualResources") or [])}}, None
    if _brief_pending(handle):
        # Observed with Orca 1.4.222: the brief pasted into an adopted OMP terminal can stay in the input box.
        orca_raw("terminal", "send", "--terminal", handle, "--enter")
        data.setdefault("result", {})["briefSubmitted"] = True
    return True, data, wid.split("::", 1)[1]


BRIEF_LEAD = "Please carry out this task from my Orca coordinator"


def _brief_pending(handle: str) -> bool:
    """True when Orca's brief still sits in the worker's input box (the last lines of the screen)."""
    orca_raw("terminal", "wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms", "15000", timeout=30)
    ok, data = orca_raw("terminal", "read", "--terminal", handle, "--screen")
    tail = [line for line in ((data.get("result") or {}).get("terminal") or {}).get("tail") or [] if line.strip()]
    return ok and any(BRIEF_LEAD in line for line in tail[-3:])


def _require_chat_in_repo(repo_path: str) -> None:
    """Workers must be child worktrees of the triage chat. `worker-start --worktree new-child` takes the parent
    from the caller's Orca terminal (ORCA_WORKTREE_ID), not from the shell cwd. Orca 1.4.222 links a child only
    inside the same repo and project (`worktree set --parent-worktree` across repos answers
    LINEAGE_PARENT_CONTEXT_CONFLICT), so a chat in another repo would get a top-level worker. Refuse before any
    Orca change."""
    chat = (os.environ.get("ORCA_WORKTREE_ID") or "").partition("::")[2]
    if not chat:
        raise Refused("ORCA_WORKTREE_ID is not set: run start-worker from the triage chat's Orca terminal")
    if _git_common_dir(chat) != _git_common_dir(repo_path):
        raise Refused(f"the triage chat's worktree {chat} is not in this project's repo {repo_path}, so the worker"
                      f" would not be its child worktree. Do not start the worker and do not edit {repo_path}"
                      f" yourself: ask the user where this project's code should live")


def cmd_review(db, a) -> dict:
    task = get_task(db, a.task)
    if task["stage"] != "reviewing":
        raise Refused(f"task {a.task} is in stage '{task['stage']}'; review needs 'reviewing'")
    pin = _current_pin(db, task)
    if pin != task["reviewed_sha"]:
        set_stage(db, a.task, "verifying", ("reviewing",), {"reviewed_sha": task["reviewed_sha"], "tip": pin})
        return {"task_id": a.task, "stage": "verifying",
                "next": f"the branch moved from {task['reviewed_sha'][:12]} to {pin[:12]}; verify again"}
    db.execute("INSERT INTO reviews (task_id, kind, verdict, evidence, created_at) VALUES (?,?,?,?,?)",
               (a.task, a.kind, a.verdict, a.evidence, iso(now())))
    log_event(db, "task", a.task, f"review:{a.kind}:{a.verdict}", {"evidence": a.evidence, "sha": pin})
    if a.verdict == "fail":
        set_stage(db, a.task, "rework", ("reviewing",), {"kind": a.kind})
        return {"task_id": a.task, "stage": "rework", "next": _rework_hint(task)}
    # Count only reviews after the latest passing verification.
    since = db.execute("SELECT max(review_id) FROM reviews WHERE task_id=? AND kind='verification'"
                       " AND verdict='pass'", (a.task,)).fetchone()[0]
    passed = {r["kind"] for r in db.execute(
        "SELECT kind FROM reviews WHERE task_id=? AND review_id>? AND verdict='pass'", (a.task, since))}
    missing = [k for k in REVIEW_KINDS if k not in passed]
    if missing:
        return {"task_id": a.task, "stage": "reviewing", "waiting_for": missing}
    title = task["title"] or a.task
    marker = f"Human merge gate for {a.task} "
    # Reuse a merge Task and gate that a crashed earlier `review` created in Orca but never recorded here.
    merge = next((t for t in orca("orchestration", "task-list", "--run", task["run_id"]).get("tasks", [])
                  if (t.get("spec") or "").startswith(marker) and t.get("status") not in ("failed", "completed")),
                 None)
    if merge is None:
        merge = orca("orchestration", "task-create", "--run", task["run_id"],
                     "--task-title", f"merge: {title}"[:120],
                     "--spec", f"{marker}({title}). Coordinator merges only after"
                               f" `orca-lifecycle.py approve --task {a.task}`. Never dispatch this task.",
                     "--deps", json.dumps([a.task]))["task"]
    gate = next((g for g in orca("orchestration", "gate-list", "--run", task["run_id"], "--task", merge["id"],
                                 "--status", "pending").get("gates", [])), None)
    if gate is None:
        gate = orca("orchestration", "gate-create", "--task", merge["id"],
                    "--question", f"Approve {'merge' if task['kind'] == 'code' else 'report'} of {a.task}:"
                                  f" {title} at {pin[:12]}?",
                    "--options", json.dumps(["approve", "reject"]))["gate"]
    db.execute("UPDATE tasks SET merge_task_id=?, merge_gate_id=? WHERE task_id=?",
               (merge["id"], gate["id"], a.task))
    set_stage(db, a.task, "awaiting_approval", ("reviewing",),
              {"merge_task_id": merge["id"], "gate_id": gate["id"]})
    return {"task_id": a.task, "stage": "awaiting_approval", "gate_id": gate["id"],
            "next": f"orca-lifecycle.py approve --task {a.task} --by <name> --answer \"<verbatim answer>\""}


def _resolve_merge_gate(task: sqlite3.Row, verdict: str, resolution: str) -> None:
    """Resolve the Orca merge gate once. A retry after a crash finds it already resolved."""
    gates = orca("orchestration", "gate-list", "--run", task["run_id"], "--task", task["merge_task_id"]).get("gates", [])
    gate = next((g for g in gates if g["id"] == task["merge_gate_id"]), None)
    if gate is None:
        raise Refused(f"Orca has no gate {task['merge_gate_id']} on task {task['merge_task_id']}")
    if gate["status"] == "pending":
        orca("orchestration", "gate-resolve", "--id", gate["id"], "--resolution", resolution)
    elif not (gate.get("resolution") or "").startswith(verdict):
        raise Refused(f"gate {gate['id']} is already '{gate['status']}' with resolution {gate.get('resolution')!r}")


def cmd_approve(db, a) -> dict:
    task = get_task(db, a.task)
    if task["stage"] != "awaiting_approval" or not task["merge_gate_id"]:
        raise Refused(f"task {a.task} is in stage '{task['stage']}'; approve needs 'awaiting_approval'")
    pin = _current_pin(db, task)
    if pin != task["reviewed_sha"]:
        raise Refused(f"the branch moved from {task['reviewed_sha'][:12]} to {pin[:12]} after review;"
                      f" run `reject --task {a.task} --by <name> --answer \"<verbatim answer>\""
                      f" --reason \"branch moved\"`, then retry the task")
    _resolve_merge_gate(task, "approve", f"approve (by {a.by}) at {pin[:12]}")
    db.execute("INSERT INTO approvals (task_id, gate_id, commit_sha, resolved_by, answer, resolved_at)"
               " VALUES (?,?,?,?,?,?)", (a.task, task["merge_gate_id"], pin, a.by, a.answer, iso(now())))
    set_stage(db, a.task, "approved", ("awaiting_approval",),
              {"resolved_by": a.by, "answer": a.answer, "commit_sha": pin})
    finish = "merge" if task["kind"] == "code" else "accept"
    return {"task_id": a.task, "stage": "approved", "next": f"orca-lifecycle.py {finish} --task {a.task}"}


def cmd_reject(db, a) -> dict:
    task = get_task(db, a.task)
    if task["stage"] not in ("awaiting_approval", "approved") or not task["merge_gate_id"]:
        raise Refused(f"task {a.task} is in stage '{task['stage']}'; reject needs 'awaiting_approval' or 'approved'")
    if task["stage"] == "awaiting_approval":
        _resolve_merge_gate(task, "reject", f"reject (by {a.by}): {a.reason}")
    # From `approved` the gate already says approve; the failed merge Task and the event record the reversal.
    orca("orchestration", "task-update", "--id", task["merge_task_id"], "--status", "failed",
         "--result", f"rejected (by {a.by}): {a.reason}", "--run", task["run_id"])
    db.execute("UPDATE tasks SET merge_task_id=NULL, merge_gate_id=NULL WHERE task_id=?", (a.task,))
    db.execute("DELETE FROM approvals WHERE task_id=?", (a.task,))
    set_stage(db, a.task, "rework", ("awaiting_approval", "approved"),
              {"rejected_by": a.by, "reason": a.reason, "answer": a.answer})
    return {"task_id": a.task, "stage": "rework", "next": _rework_hint(task)}


def _approved(db, task: sqlite3.Row, kind: str) -> str:
    approval = db.execute("SELECT * FROM approvals WHERE task_id=?", (task["task_id"],)).fetchone()
    if task["stage"] != "approved" or approval is None:
        raise Refused(f"task {task['task_id']} is in stage '{task['stage']}'; it needs a human approval"
                      f" (`approve --task {task['task_id']} --by <name> --answer \"<verbatim answer>\"`)")
    if task["kind"] != kind:
        other = "accept" if task["kind"] == "report" else "merge"
        raise Refused(f"task {task['task_id']} is a {task['kind']} task; use `{other} --task {task['task_id']}`")
    return approval["commit_sha"]


def _only_stored_report_dirty(db, task_id: str, wt: str, sha: str) -> bool:
    """True when the worktree's only change is the report file whose content matches the stored hash."""
    row = db.execute("SELECT path FROM reports WHERE task_id=? AND sha256=?", (task_id, sha)).fetchone()
    status = git(wt, "status", "--porcelain", "--untracked-files=all", check=False)
    if row is None or status.returncode != 0:
        return False
    dirty = [ln[3:] for ln in status.stdout.splitlines() if ln.strip()]
    report = Path(row["path"]).resolve()
    if [str((Path(wt) / d).resolve()) for d in dirty] != [str(report)] or not report.is_file():
        return False
    return hashlib.sha256(report.read_bytes()).hexdigest() == sha


def _finish_worktrees(db, task_id: str, repo: str, remove: bool, out: dict, force_latest: bool = False) -> None:
    """After merge/accept, every worktree of the task is finished, not only the merged attempt's: Done on the
    board (set_stage already moved the latest one), then terminals closed, or the worktree deleted when asked."""
    latest = latest_attempt(db, task_id, "completed")
    seen: set[str] = set()
    for att in db.execute("SELECT * FROM attempts WHERE task_id=? AND worktree_path IS NOT NULL"
                          " ORDER BY created_at, rowid", (task_id,)).fetchall():
        wt = att["worktree_path"]
        if wt in seen or Path(wt).resolve() == Path(repo).resolve() or not Path(wt).is_dir():
            continue
        seen.add(wt)
        is_latest = latest is not None and att["dispatch_id"] == latest["dispatch_id"]
        if not is_latest:
            set_board_status(db, att["dispatch_id"], wt, "completed")
        force = force_latest and is_latest
        if remove:
            args, done, failed = ("worktree", "rm", "--worktree", f"path:{wt}", *(["--force"] if force else [])), \
                "worktree_removed", "worktree_remove_failed"
        else:
            args, done, failed = ("terminal", "close", "--worktree", f"path:{wt}", "--all"), \
                "terminals_closed", "terminals_close_failed"
        try:
            orca(*args)
            log_event(db, "attempt", att["dispatch_id"], done, {"path": wt, "force": force})
            out.setdefault(done, []).append(wt)
        except OrcaError as exc:
            log_event(db, "attempt", att["dispatch_id"], failed, {"error": str(exc)})
            out.setdefault(failed, []).append(str(exc))


def cmd_accept(db, a) -> dict:
    """Finish an approved report task: the stored report is the deliverable. No git merge."""
    task = get_task(db, a.task)
    approved = _approved(db, task, "report")
    if approved != task["reviewed_sha"]:
        raise Refused(f"approval {approved[:12]} does not match the reviewed report {task['reviewed_sha'][:12]}")
    orca("orchestration", "task-update", "--id", task["merge_task_id"], "--status", "completed",
         "--result", f"accepted report {approved}", "--run", task["run_id"])
    set_stage(db, a.task, "merged", ("approved",), {"report_sha": approved, "accepted": True})
    # The worktree may be deleted below, so the accepted report must live somewhere stable first.
    row = db.execute("SELECT content FROM reports WHERE task_id=? AND sha256=?", (a.task, approved)).fetchone()
    export_dir = Path(a.export_dir or os.environ.get("ORCA_REPORTS_DIR") or REPORTS_DIR)
    export_dir.mkdir(parents=True, exist_ok=True)
    exported = export_dir / f"{a.task}-{approved[:12]}.md"
    exported.write_text(row["content"], encoding="utf-8")
    log_event(db, "task", a.task, "report_exported", {"path": str(exported), "sha256": approved})
    out = {"task_id": a.task, "stage": "merged", "report_sha": approved, "report": str(exported)}
    attempt = latest_attempt(db, a.task, "completed")
    # The report is untracked in the worktree, so a plain `worktree rm` refuses. Force only when that file is
    # the sole change and matches the stored copy: then nothing is lost.
    force = bool(attempt and attempt["worktree_path"] and Path(attempt["worktree_path"]).is_dir()
                 and _only_stored_report_dirty(db, a.task, attempt["worktree_path"], approved))
    _finish_worktrees(db, a.task, get_project(db, task["project_id"])["repo_path"], a.remove_worktree, out, force)
    out["next"] = f"orca-lifecycle.py capture --task {a.task}"
    return out


def cmd_merge(db, a) -> dict:
    task = get_task(db, a.task)
    approved = _approved(db, task, "code")
    attempt = latest_attempt(db, a.task, "completed")
    if attempt is None or not attempt["branch"]:
        raise Refused(f"task {a.task} has no completed attempt with a branch")
    repo = get_project(db, task["project_id"])["repo_path"]
    branch = attempt["branch"]
    into = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if a.into and a.into != into:
        raise Refused(f"{repo} has '{into}' checked out, not '{a.into}'")
    if branch == into:
        raise Refused(f"worker branch '{branch}' is the merge target itself")
    title = task["title"] or a.task
    # Resume: a crash after `git merge` leaves our merge commit on HEAD's first-parent chain.
    # This check needs only the approved commit, because Orca can delete the branch after the merge.
    prior = next(iter(git(repo, "log", "--first-parent", "--format=%H", "--fixed-strings",
                          f"--grep=merge({a.task}):").stdout.split()), "")
    kept_dirty: list[str] = []
    if prior and git(repo, "merge-base", "--is-ancestor", approved, prior, check=False).returncode == 0:
        sha = prior
    else:
        _, _, tip = _branch_tip(db, a.task)
        if tip != approved:
            raise Refused(f"branch '{branch}' moved from the approved {approved[:12]} to {tip[:12]};"
                          f" the new commits have no review or approval. To merge what was approved,"
                          f" run `git branch -f {branch} {approved}`, then merge again")
        ahead = int(git(repo, "rev-list", "--count", f"{into}..{approved}").stdout.strip())
        if ahead == 0:
            raise Refused(f"branch '{branch}' has no commits that are not already in '{into}';"
                          f" run `reject --task {a.task} --by <name> --answer \"<verbatim answer>\" --reason \"...\"`")
        # Owner work in progress may sit in the checkout. Merge around it: refuse only when a dirty or untracked
        # file is one the branch changes, or when the merge itself conflicts. Nothing here touches those files.
        changed = set(git(repo, "diff", "--name-only", "--no-renames", f"{into}...{approved}").stdout.split("\n"))
        status = [line for line in git(repo, "status", "--porcelain", "--no-renames").stdout.split("\n") if line]
        dirty = sorted({line[3:] for line in status})
        overlap = sorted(set(dirty) & changed)
        if overlap:
            raise Refused(f"uncommitted changes in {repo} touch files that the branch changes: {', '.join(overlap)}."
                          f" Find the agent or person who owns them, let them commit or finish, then merge again")
        trial = git(repo, "merge-tree", "--write-tree", "--name-only", into, approved, check=False)
        if trial.returncode != 0:
            raise Refused(f"merging {branch} into {into} conflicts in: {trial.stdout.strip()[:300]}."
                          f" Run `reject --task {a.task} --by <name> --answer \"<verbatim answer>\""
                          f" --reason \"merge conflict\"`, then retry the task on top of '{into}'")
        merged = git(repo, "merge", "--no-ff", "-m", f"merge({a.task}): {title}", approved, check=False)
        if merged.returncode != 0:
            if not dirty:  # --abort cannot always restore uncommitted work, so never run it over someone's files
                git(repo, "merge", "--abort", check=False)
            raise Refused(f"merge of {branch} into {into} failed: {merged.stdout.strip()[:300]}"
                          f"{'' if not dirty else '. The merge was not aborted, because the checkout has uncommitted files; resolve it by hand'}")
        kept_dirty = sorted(line[3:] for line in status if not line.startswith("??"))
        sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "merge-base", "--is-ancestor", approved, sha)
    orca("orchestration", "task-update", "--id", task["merge_task_id"], "--status", "completed",
         "--result", f"merged {branch} into {into} at {sha}", "--run", task["run_id"])
    set_stage(db, a.task, "merged", ("approved",),
              {"sha": sha, "branch": branch, "into": into, "approved_sha": approved})
    out = {"task_id": a.task, "stage": "merged", "sha": sha, "branch": branch, "into": into, "kept_dirty": kept_dirty}
    _finish_worktrees(db, a.task, repo, a.remove_worktree, out)
    out["next"] = f"orca-lifecycle.py capture --task {a.task}"
    return out


def _summary(body: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(DECISION_RE.sub("", body).split()))
    return " ".join(sentences[:3]).strip()


def cmd_capture(db, a) -> dict:
    task = get_task(db, a.task)
    if task["stage"] != "merged":
        raise Refused(f"task {a.task} is in stage '{task['stage']}'; capture needs 'merged'")
    attempt = latest_attempt(db, a.task, "completed")
    done_evidence = _worker_done_evidence(db, attempt["dispatch_id"]) if attempt else {}
    body = done_evidence.get("body", "")
    if a.report:
        report = sys.stdin.read() if a.report == "-" else Path(a.report).read_text(encoding="utf-8")
    elif task["kind"] == "report":
        row = db.execute("SELECT content FROM reports WHERE task_id=? AND sha256=?",
                         (a.task, task["reviewed_sha"])).fetchone()
        report = row["content"] if row else ""
    else:
        path = (done_evidence.get("payload") or {}).get("reportPath")
        report = Path(path).read_text(encoding="utf-8") if path and Path(path).is_file() else ""
    decisions = list(dict.fromkeys(DECISION_RE.findall(report + "\n" + body)))
    sot = list(dict.fromkeys((m[0].lower(), m[1]) for m in SOT_RE.findall(report + "\n" + body)))
    project = get_project(db, task["project_id"])
    context_dir = Path(project["context_dir"]).resolve()
    target = (context_dir / a.file).resolve()
    if not target.is_relative_to(context_dir):
        raise Refused(f"{a.file} is outside the project context folder {context_dir}")
    if decisions and not target.is_file():
        if a.file != DECISIONS_LOG:
            raise Refused(f"{target} does not exist; only the operational log `{DECISIONS_LOG}` is created here")
        target.write_text("# Decisions\n\nOperational log: one short line per decision. Live state lives in the"
                          " architecture, business, dev, and product documents.\n", encoding="utf-8")
    # Idempotent retry: a crash after the append leaves this task's lines in the log.
    already = decisions and f"({a.task}):" in target.read_text(encoding="utf-8")
    if decisions and not already:
        day, title = now().date().isoformat(), task["title"] or a.task
        with target.open("a", encoding="utf-8") as fh:
            fh.write("".join(f"- {day} · {title} ({a.task}): {d}\n" for d in decisions))
    if decisions:
        _commit_if_tracked(project["repo_path"], [target], f"docs(decisions): {a.task}")
    gbrain = []
    if a.gbrain and decisions:
        exe = shutil.which("gbrain")
        for d in decisions:
            if not exe:
                gbrain.append({"decision": d, "error": "gbrain not on PATH"})
                continue
            proc = subprocess.run([exe, "capture", f"{task['title'] or a.task}: {d}"],
                                  capture_output=True, text=True, timeout=120)
            gbrain.append({"decision": d, "exit": proc.returncode})
    merged = db.execute("SELECT evidence_json FROM events WHERE entity_type='task' AND entity_id=?"
                        " AND transition='stage:merged' ORDER BY event_id DESC LIMIT 1", (a.task,)).fetchone()
    m = json.loads(merged["evidence_json"]) if merged else {}
    if m.get("accepted"):
        outcome = f"Accepted report {m.get('report_sha', '')[:12]}."
    else:
        outcome = f"Merged {m.get('branch')} into {m.get('into')} at {m.get('sha', '')[:12]}." if merged else ""
    summary = a.summary or " ".join(p for p in (_summary(body), outcome) if p)
    for area, note in sot:
        db.execute("INSERT OR IGNORE INTO sot_updates (task_id, area, note) VALUES (?,?,?)", (a.task, area, note))
    log_event(db, "task", a.task, "captured", {"summary": summary, "decisions": decisions,
                                               "file": str(target) if decisions else None, "gbrain": gbrain})
    out = {"task_id": a.task, "decisions_appended": len(decisions), "file": str(target) if decisions else None,
           "summary": summary, "gbrain": gbrain}
    pending = _pending_sot(db, a.task)
    if pending:
        out.update(stage="merged", sot_pending=pending, next=_sot_hint(a.task, pending, context_dir))
        return out
    _close(db, a.task, summary, decisions)
    out["stage"] = "closed"
    return out


def _pending_sot(db, task_id: str) -> list[dict]:
    return [{"area": r["area"], "note": r["note"]} for r in db.execute(
        "SELECT area, note FROM sot_updates WHERE task_id=? AND done_at IS NULL ORDER BY area, note", (task_id,))]


def _sot_hint(task_id: str, pending: list[dict], context_dir: Path) -> str:
    areas = sorted({p["area"] for p in pending})
    return (f"update the live SoT documents in {context_dir} for {', '.join(areas)} (rewrite them to the new"
            f" reality), then `orca-lifecycle.py sot-done --task {task_id} --file <doc> [--file <doc> ...]`")


def _commit_if_tracked(repo: str, paths: list[Path], message: str) -> list[str]:
    """Commit only files git already tracks; untracked project folders stay as they are."""
    tracked = [str(p) for p in paths
               if git(repo, "ls-files", "--error-unmatch", "--", str(p), check=False).returncode == 0]
    if tracked and git(repo, "status", "--porcelain", "--", *tracked).stdout.strip():
        git(repo, "commit", "-q", "-m", message, "--", *tracked)
    return tracked


def _close(db, task_id: str, summary: str, decisions: list[str], sot: list[dict] | None = None) -> None:
    log_event(db, "task", task_id, "run_completed", {"summary": summary, "decisions": decisions, "sot": sot or []})
    set_stage(db, task_id, "closed", ("merged",))


def cmd_sot_done(db, a) -> dict:
    """Record that the triage agent brought the live SoT documents up to date, then close the task."""
    task = get_task(db, a.task)
    captured = db.execute("SELECT evidence_json, timestamp FROM events WHERE entity_type='task' AND entity_id=?"
                          " AND transition='captured' ORDER BY event_id DESC LIMIT 1", (a.task,)).fetchone()
    if task["stage"] != "merged" or captured is None:
        raise Refused(f"task {a.task} is in stage '{task['stage']}'; sot-done needs a captured, merged task")
    pending = _pending_sot(db, a.task)
    if not pending:
        raise Refused(f"task {a.task} has no pending SoT updates")
    project = get_project(db, task["project_id"])
    context_dir = Path(project["context_dir"]).resolve()
    since = parse_time(captured["timestamp"])
    files = []
    for f in a.file:
        p = Path(f) if Path(f).is_absolute() else context_dir / f
        p = p.resolve()
        if not p.is_relative_to(context_dir) or not p.is_file():
            raise Refused(f"{f} is not an existing file inside the project folder {context_dir}")
        changed = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc).replace(microsecond=0)
        if changed < since:
            raise Refused(f"{p} was not changed after capture ({iso(changed)} < {iso(since)}); update it first")
        files.append(p)
    rel = [str(p.relative_to(context_dir)).lower() for p in files]
    stem = {"architecture": "architect", "business": "business", "dev": "dev", "product": "product"}
    missing = sorted({x["area"] for x in pending if not any(stem[x["area"]] in r for r in rel)})
    if missing:
        raise Refused(f"no updated document covers {missing}; name the {', '.join(missing)} document(s) with --file")
    areas = sorted({x["area"] for x in pending})
    committed = _commit_if_tracked(project["repo_path"], files, f"docs(sot): {a.task} {', '.join(areas)}")
    names = json.dumps([str(p) for p in files])
    db.execute("UPDATE sot_updates SET files=?, done_at=? WHERE task_id=? AND done_at IS NULL",
               (names, iso(now()), a.task))
    evidence = json.loads(captured["evidence_json"])
    _close(db, a.task, evidence["summary"], evidence["decisions"], pending)
    return {"task_id": a.task, "stage": "closed", "sot_updated": pending, "files": [str(p) for p in files],
            "committed": committed}


def cmd_gate(db, a) -> dict:
    """Resolve a non-merge decision gate with the human's recorded answer. Merge gates go through approve/reject."""
    merge_gate = db.execute("SELECT task_id FROM tasks WHERE merge_gate_id=?", (a.id,)).fetchone()
    if merge_gate:
        raise Refused(f"{a.id} is the merge gate of {merge_gate['task_id']}; use `approve` or `reject`")
    orca("orchestration", "gate-resolve", "--id", a.id, "--resolution", f"{a.resolution} (by {a.by})")
    log_event(db, "gate", a.id, "resolved", {"resolution": a.resolution, "by": a.by, "answer": a.answer})
    return {"gate": a.id, "resolution": a.resolution, "by": a.by}


def cmd_answer(db, a) -> dict:
    thread = question_thread(a.question) or {}
    orca("orchestration", "reply", "--id", a.question, "--body", a.body)
    log_event(db, "question", a.question, "answered", {
        "task_id": thread.get("task_id"), "dispatch_id": thread.get("dispatch_id"), "body": a.body})
    return {"question": a.question, "task_id": thread.get("task_id"), "dispatch_id": thread.get("dispatch_id")}


def cmd_discard(db, a) -> dict:
    """Drop one message that ingest cannot process, with the reason on record. The next `wait` (or `ingest` of
    the same batch) skips it, so the rest of its batch is recorded and acked."""
    if not a.reason.strip():
        raise Refused("--reason must say why the message is dropped")
    _, applied = append_event(db, "message", a.message, "discarded", {"reason": a.reason})
    return {"message": a.message, "discarded": True, "already": not applied,
            "next": "orca-lifecycle.py wait (Orca delivers the batch again; it is acked this time)"}


def cmd_recover(db, a) -> dict:
    stale = db.execute("SELECT a.*, t.run_id, t.project_id FROM attempts a JOIN tasks t USING(task_id)"
                       " WHERE a.status='active' AND a.lease_expires_at < ?", (iso(now()),)).fetchall()
    workers: dict[str, dict[str, dict]] = {}
    decisions = []
    for att in stale:
        run = att["run_id"]
        if run not in workers:
            try:
                workers[run] = orca_workers(run)
            except OrcaError as exc:
                workers[run] = None
                decisions.append({"run_id": run, "orca_error": str(exc)})
        if workers[run] is None:
            # Liveness unknown: absence of evidence never authorizes abandon or retry.
            decisions.append({"dispatch_id": att["dispatch_id"], "task_id": att["task_id"],
                              "verdict": "liveness_unknown"})
            continue
        w = workers[run].get(att["dispatch_id"]) or {}
        liveness = ((w.get("projection") or {}).get("liveness") or {}).get("verdict", "unknown")
        repo = get_project(db, att["project_id"])["repo_path"]
        branch, wt = att["branch"], att["worktree_path"]
        wt_ok = bool(wt) and Path(wt).is_dir() and (
            not branch or git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() == branch)
        branch_ok = bool(branch) and git(repo, "rev-parse", "--verify", "--quiet",
                                         f"refs/heads/{branch}", check=False).returncode == 0
        heartbeat = parse_time(att["heartbeat_at"])
        heartbeat_stale = heartbeat is None or now() - heartbeat > STALE_HEARTBEAT
        evidence = {"liveness": liveness, "dispatch_status": w.get("dispatchStatus"), "branch": branch,
                    "worktree_present": wt_ok, "branch_present": branch_ok,
                    "heartbeat_at": att["heartbeat_at"]}
        task_id, dispatch_id = att["task_id"], att["dispatch_id"]
        done_signal = w.get("dispatchStatus") == "completed" or (branch and pr_exists(repo, branch))
        report_msg = orca_worker_done(dispatch_id) if done_signal else None
        try:
            payload = json.loads(report_msg["payload"]) if report_msg else None
        except json.JSONDecodeError:
            payload = None
        outcome = payload.get("outcome") if isinstance(payload, dict) else None
        if outcome in ("succeeded", "failed"):
            # Settle through the same event as ingest, so the worker report stays available to `capture`.
            _, applied = append_event(db, "attempt", dispatch_id, "worker_done",
                                      {"task_id": task_id, "payload": payload, "body": report_msg["body"]})
            log_event(db, "attempt", dispatch_id, "crash_recovery_completed",
                      {**evidence, "message_id": report_msg["id"]})
            if applied and get_task(db, task_id)["stage"] in SETTLEABLE:
                _apply_worker_done(db, task_id, dispatch_id, outcome)
            verdict = "completed"
        elif done_signal:
            # Orca or a PR says done, but no valid worker report exists to capture from. Leave the state alone.
            verdict = "completed_without_report"
        elif liveness == "live":
            append_event(db, "attempt", dispatch_id, "lease_expired_live", evidence)
            verdict = "live_lease_expired"
        elif not wt_ok and not branch_ok:
            db.execute("UPDATE attempts SET status='failed' WHERE dispatch_id=?", (dispatch_id,))
            log_event(db, "attempt", dispatch_id, "crash_recovery_failed", {**evidence, "worktree_missing": True})
            set_stage(db, task_id, "failed", ("implementing",), {"dispatch_id": dispatch_id})
            verdict = "failed"
        elif liveness == "exited" or w.get("dispatchStatus") == "failed":
            db.execute("UPDATE attempts SET status='abandoned' WHERE dispatch_id=?", (dispatch_id,))
            log_event(db, "attempt", dispatch_id, "crash_recovery_abandoned",
                      {**evidence, "branch_preserved": branch})
            set_stage(db, task_id, "abandoned", ("implementing",), {"dispatch_id": dispatch_id})
            verdict = "abandoned"
        else:
            # No positive exit proof. A stale heartbeat is a reason to inspect, not to abandon.
            verdict = "stale_unverified" if heartbeat_stale else "waiting"
        decisions.append({"dispatch_id": dispatch_id, "task_id": task_id, "verdict": verdict, **evidence})
    return {"checked": len(stale), "decisions": decisions}


def _ticket_key(name: str) -> str:
    m = re.match(r"(\d+)", Path(name).name)
    if not m:
        return Path(name).stem
    return m.group(1).lstrip("0") or "0"


def _parse_ticket(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), path.stem)
    fields: dict[str, str] = {}
    for ln in lines[:40]:
        m = re.match(r"^(Type|Status|Blocked by):\s*(.*)$", ln.strip(), re.IGNORECASE)
        if m and m.group(1).lower() not in fields:
            fields[m.group(1).lower()] = m.group(2).strip()
    blockers = [b for b in re.split(r"[,\s]+", fields.get("blocked by", ""))
                if b and b.lower() not in ("none", "-", "n/a")]
    acc = re.search(r"^##\s+Acceptance[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    return {"path": str(path.resolve()), "key": _ticket_key(path.name), "title": title, "text": text,
            "type": fields.get("type", "").lower(), "status": fields.get("status", "").lower(),
            "blockers": [_ticket_key(b) for b in blockers],
            "acceptance": acc.group(1).strip() if acc and acc.group(1).strip() else None}


def cmd_import_tickets(db, a) -> dict:
    """Turn `ready-for-agent` tickets of one `.scratch/<feature>/issues/` folder into tracked Orca Tasks."""
    get_project(db, a.project)
    issues = Path(a.dir) / "issues"
    if not issues.is_dir():
        raise Refused(f"{issues} does not exist")
    tickets = [_parse_ticket(p) for p in sorted(issues.glob("*.md"))]
    listed = orca("orchestration", "task-list", "--run", a.run).get("tasks", [])
    task_of: dict[str, str] = {}
    resolved = {t["key"] for t in tickets if t["status"].startswith("resolved")}
    out: dict = {"created": [], "existing": [], "skipped": []}
    pending = []
    for t in tickets:
        row = db.execute("SELECT task_id FROM tickets WHERE ticket_path=?", (t["path"],)).fetchone()
        if row:
            task_of[t["key"]] = row["task_id"]
            out["existing"].append({"ticket": t["path"], "task_id": row["task_id"]})
        elif t["status"].startswith("ready-for-agent"):
            pending.append(t)
        else:
            out["skipped"].append({"ticket": t["path"], "status": t["status"] or "(none)"})
    # Import in dependency order, not file order: `01` may be blocked by `02`.
    while pending:
        ready = [t for t in pending if all(b in resolved or b in task_of for b in t["blockers"])]
        if not ready:
            break
        for t in ready:
            pending.remove(t)
            deps = [task_of[b] for b in t["blockers"] if b in task_of]
            head = f"Ticket: {t['path']}\n"
            body = t["text"] if len(head) + len(t["text"]) + len(WORKER_CONTRACT) < SPEC_LIMIT else \
                "The ticket is longer than the spec limit. Read the ticket file named above: it is the full spec.\n"
            spec = f"{head}\n{body}\n{WORKER_CONTRACT}"
            # Reuse a Task that a crashed earlier import created in Orca but never recorded here.
            existing = next((x for x in listed if (x.get("spec") or "").startswith(head)), None)
            task_id = existing["id"] if existing else orca(
                "orchestration", "task-create", "--run", a.run, "--task-title", t["title"][:120], "--spec", spec,
                "--deps", json.dumps(deps))["task"]["id"]
            kind = a.kind or ("report" if t["type"] == "research" else "code")
            track_task(db, task_id, a.run, a.project,
                       t["acceptance"] or f"See the acceptance section of {t['path']}", kind, t["title"], "large")
            db.execute("INSERT INTO tickets (ticket_path, task_id) VALUES (?,?)", (t["path"], task_id))
            task_of[t["key"]] = task_id
            out["created"].append({"ticket": t["path"], "task_id": task_id, "kind": kind, "deps": deps})
    for t in pending:
        missing = [b for b in t["blockers"] if b not in resolved and b not in task_of]
        out["skipped"].append({"ticket": t["path"], "status": t["status"],
                               "reason": f"blocked by ticket(s) {missing}, which are neither resolved nor imported"})
    ready_now = [c["task_id"] for c in out["created"] if not c["deps"]]
    out["next"] = [f"orca-lifecycle.py start-worker --task {t} --class large --agent omp" for t in ready_now]
    return out


def cmd_triage_here(db, a) -> dict:
    """Mark a folder as the triage chat's home: the orca-triage OMP extension activates there."""
    path = Path(os.environ.get("ORCA_TRIAGE_CONFIG") or TRIAGE_CONFIG)
    cfg = json.loads(path.read_text()) if path.is_file() else {}
    folder = str(Path(a.dir or os.getcwd()).resolve())
    dirs = [d for d in cfg.get("triage_dirs", []) if d != folder]
    if not a.remove:
        dirs.append(folder)
    cfg["triage_dirs"] = dirs
    cfg["lifecycle"] = a.lifecycle or cfg.get("lifecycle") or str(INSTALLED_LIFECYCLE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)
    return {"config": str(path), "triage_dirs": dirs, "lifecycle": cfg["lifecycle"]}


NEXT = {
    "routed": (6, "start a worker: {retry}"),
    "verifying": (4, "run the verification for {t} ({kind}), then `verify --task {t} --verdict pass|fail`"),
    "reviewing": (4, "run judge (reviewer) and board (council-strategist), then `review --task {t}`"),
    "rework": (3, "run `improve` on {t}, then {retry}"),
    "awaiting_approval": (2, "orca-lifecycle.py approve --task {t} --by <name> --answer \"<verbatim answer>\""
                             "   (or reject)"),
    "approved": (3, "orca-lifecycle.py {finish} --task {t}   (if it refuses: reject, then retry)"),
    "merged": (3, "orca-lifecycle.py capture --task {t}"),
    "abandoned": (3, "inspect `git log {b}` and `git diff HEAD...{b}`, then {retry}; or cancel in Orca with"
                     " `task-update --id {t} --run {r} --status failed --result cancelled`"),
    "failed": (3, "worktree and branch are gone: {retry}; or cancel in Orca with"
                  " `task-update --id {t} --run {r} --status failed --result cancelled`"),
    "implementing": (5, "orca-lifecycle.py wait"),
}


def attention_rows(db) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    open_tasks = db.execute("SELECT * FROM tasks WHERE stage != 'closed'").fetchall()
    runs = sorted({t["run_id"] for t in open_tasks})
    by_task = {t["task_id"]: t for t in open_tasks}
    orca_tasks: dict[str, dict] = {}
    for q in pending_questions(runs):
        rows.append({"priority": 1, "kind": "question", "task_id": q["task_id"], "run_id": q["run_id"],
                     "stage": by_task[q["task_id"]]["stage"] if q["task_id"] in by_task else None,
                     "detail": (q["body"] or "")[:200],
                     "next": f"orca-lifecycle.py answer --question {q['message_id']} --body \"<answer>\""})
    merge_tasks = {t["merge_task_id"] for t in open_tasks if t["merge_task_id"]}
    workers: dict[str, dict] = {}
    for run in runs:
        try:
            for g in orca("orchestration", "gate-list", "--run", run, "--status", "pending").get("gates", []):
                if g["task_id"] not in merge_tasks:
                    rows.append({"priority": 1, "kind": "gate", "task_id": g["task_id"], "run_id": run,
                                 "stage": None, "detail": g["question"][:200],
                                 "next": f"orca-lifecycle.py gate --id {g['id']} --resolution \"<choice>\""
                                         " --by <name> --answer \"<verbatim answer>\""})
            workers.update(orca_workers(run))
            orca_tasks.update({t["id"]: t for t in
                               orca("orchestration", "task-list", "--run", run).get("tasks", [])})
        except OrcaError as exc:
            errors.append(str(exc))
    for t in open_tasks:
        orca_task = orca_tasks.get(t["task_id"]) or {}
        if t["stage"] in ("failed", "abandoned") and orca_task.get("status") == "failed" \
                and (orca_task.get("result") or "").startswith("cancelled"):
            continue  # cancelled in Orca: nothing left to decide
        prio, hint = NEXT[t["stage"]]
        pending = _pending_sot(db, t["task_id"]) if t["stage"] == "merged" else []
        if pending:
            prio = 2
            hint = _sot_hint(t["task_id"], pending, Path(get_project(db, t["project_id"])["context_dir"]))
            hint = hint.replace("{", "{{").replace("}", "}}")
        detail = t["title"] or ""
        if t["stage"] == "implementing":
            att = latest_attempt(db, t["task_id"], "active")
            w = workers.get(att["dispatch_id"]) if att else None
            if w:
                live = (w.get("projection") or {}).get("liveness") or {}
                detail += f" | dispatch {att['dispatch_id']} liveness={live.get('verdict')}"
                if (w.get("projection") or {}).get("attention", {}).get("requiresAction") \
                        and live.get("verdict") == "exited":
                    prio, hint = 3, "worker exited without worker_done: run `orca-lifecycle.py recover`"
        last = latest_attempt(db, t["task_id"])
        dispatch = last["dispatch_id"] if last else "<dispatch>"
        rows.append({"priority": prio, "kind": "task", "task_id": t["task_id"], "run_id": t["run_id"],
                     "stage": t["stage"], "detail": detail,
                     "next": hint.format(t=t["task_id"], d=dispatch, r=t["run_id"], kind=t["kind"],
                                         b=(last["branch"] if last else None) or "<branch>",
                                         finish="merge" if t["kind"] == "code" else "accept",
                                         retry=_retry_hint(t))})
    rows.sort(key=lambda r: (r["priority"], r["task_id"] or ""))
    return rows, errors


def cmd_status(db, a) -> dict:
    rows, errors = attention_rows(db)
    return {"generated_at": iso(now()), "rows": rows, "errors": errors}


def cmd_reload(db, a) -> dict:
    recovery = cmd_recover(db, a)
    rows, errors = attention_rows(db)
    completed = [
        {"task_id": e["entity_id"], "at": e["timestamp"], "summary": json.loads(e["evidence_json"])["summary"]}
        for e in db.execute("SELECT * FROM events WHERE transition='run_completed'"
                            " ORDER BY event_id DESC LIMIT ?", (a.limit,))]
    projects = []
    for p in db.execute("SELECT * FROM projects ORDER BY project_id"):
        files = [str(Path(p["context_dir"]) / n) for n in ("business.md", "decisions.md", "product.md", "dev.md")
                 if (Path(p["context_dir"]) / n).is_file()]
        projects.append({"project_id": p["project_id"], "context_files": files})
    return {"generated_at": iso(now()), "recovery": recovery, "rows": rows, "errors": errors,
            "recent_completions": completed, "projects": projects}


def render(name: str, out: dict) -> str:
    if name not in ("status", "reload"):
        return json.dumps(out, indent=2, ensure_ascii=False)
    lines = [f"Attention queue ({out['generated_at']})"]
    if not out["rows"]:
        lines.append("  (empty)")
    for i, r in enumerate(out["rows"], 1):
        lines.append(f"{i}. [P{r['priority']} {r['kind']}] {r['task_id'] or '-'}"
                     f" stage={r['stage'] or '-'} {r['detail']}".rstrip())
        lines.append(f"   next: {r['next']}")
    for err in out["errors"]:
        lines.append(f"orca error: {err}")
    if name == "reload":
        rec = out["recovery"]
        lines.append(f"Recovery: {rec['checked']} expired lease(s) checked")
        for d in rec["decisions"]:
            lines.append(f"  - {d.get('dispatch_id', d.get('run_id'))}: {d.get('verdict', d.get('orca_error'))}")
        lines.append("Recent completions:")
        for c in out["recent_completions"] or [{"task_id": "-", "at": "", "summary": "(none)"}]:
            lines.append(f"  - {c['task_id']} {c['at']}: {c['summary']}")
        lines.append("Project context files:")
        for p in out["projects"]:
            lines.append(f"  - {p['project_id']}: {', '.join(p['context_files']) or '(none)'}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orca-lifecycle.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--now", help="ISO8601 clock override (tests and recovery drills)")
    p.add_argument("--json", action="store_true", help="JSON output for status and reload")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("project-add", help="register a project repo")
    s.add_argument("--repo-path", required=True)
    s.add_argument("--slug")
    s.add_argument("--project-id")
    s.add_argument("--name")
    s.add_argument("--context-dir", help="folder with business.md, decisions.md, ... (default: repo)")
    s.add_argument("--base-branch", help="branch new worker worktrees start from (default: origin/HEAD, main, master)")

    s = sub.add_parser("track", help="track an existing Orca task")
    s.add_argument("--task", required=True)
    s.add_argument("--run", required=True)
    s.add_argument("--project", required=True)
    s.add_argument("--acceptance", required=True)
    s.add_argument("--kind", choices=("code", "report"), default="code")
    s.add_argument("--title")

    s = sub.add_parser("start-worker", help="create/track a task, start its worker, record the Dispatch")
    s.add_argument("--task", help="existing Orca task id (tracked, or pass --project/--run/--acceptance)")
    s.add_argument("--spec", help="create a new Orca task with this spec (the lifecycle contract is appended)")
    s.add_argument("--title")
    s.add_argument("--project")
    s.add_argument("--run")
    s.add_argument("--acceptance")
    s.add_argument("--kind", choices=("code", "report"), default="code")
    s.add_argument("--deps", help="JSON array of Orca task ids")
    s.add_argument("--agent", required=True, help="Orca agent id, for example omp, claude, codex")
    s.add_argument("--model")
    s.add_argument("--name", help="worktree name (default: <title-slug>-<task>-a<attempt>)")
    s.add_argument("--base-branch", help="override the project's base branch")
    s.add_argument("--setup", choices=("run", "skip", "inherit"))
    s.add_argument("--session", help="harness session id to record")
    s.add_argument("--class", dest="work_class", required=True, choices=("small", "large"),
                   help="ticket size label for the coordinator (imported tickets are large); every OMP worker"
                        " starts without plan mode and without an advisor")

    s = sub.add_parser("discard", help="record why an unprocessable message is dropped, so its batch can be acked")
    s.add_argument("--message", required=True, help="message id from the `errors` of ingest or wait")
    s.add_argument("--reason", required=True)

    s = sub.add_parser("wait", help="wait for worker mail, record it, then ack it")
    s.add_argument("--timeout-ms", type=int, default=900000)
    s.add_argument("--run", help="Run to check (default: the Run bound to this terminal)")

    s = sub.add_parser("import-tickets", help="turn ready-for-agent tickets into tracked Orca tasks")
    s.add_argument("--dir", required=True, help=".scratch/<feature> folder")
    s.add_argument("--run", required=True)
    s.add_argument("--project", required=True)
    s.add_argument("--kind", choices=("code", "report"), help="default: report for Type: research, else code")

    s = sub.add_parser("triage-here", help="activate the orca-triage OMP extension in this folder")
    s.add_argument("--dir", help="default: the current folder")
    s.add_argument("--lifecycle", help=f"path of orca-lifecycle.py for the extension (default {INSTALLED_LIFECYCLE})")
    s.add_argument("--remove", action="store_true")

    s = sub.add_parser("attempt", help="record an Orca Dispatch for a tracked task")
    s.add_argument("--task", required=True)
    s.add_argument("--dispatch", required=True)
    s.add_argument("--worktree-path")
    s.add_argument("--branch")
    s.add_argument("--session", help="harness session id (OMP session file or Orca session id)")

    s = sub.add_parser("ingest", help="record a `orca orchestration check --json` batch")
    s.add_argument("--file", default="-")

    s = sub.add_parser("verify", help="record the verification verdict")
    s.add_argument("--task", required=True)
    s.add_argument("--verdict", choices=("pass", "fail"), required=True)
    s.add_argument("--evidence", required=True)

    s = sub.add_parser("review", help="record a judge or board verdict")
    s.add_argument("--task", required=True)
    s.add_argument("--kind", choices=REVIEW_KINDS, required=True)
    s.add_argument("--verdict", choices=("pass", "fail"), required=True)
    s.add_argument("--evidence", required=True)

    for name in ("approve", "reject"):
        s = sub.add_parser(name, help=f"human {name} of the merge gate")
        s.add_argument("--task", required=True)
        s.add_argument("--by", required=True, help="the human who decides")
        s.add_argument("--answer", required=True, help="the human's verbatim answer, for the audit trail")
        if name == "reject":
            s.add_argument("--reason", required=True)

    s = sub.add_parser("merge", help="merge an approved code task branch (needs a human approval)")
    s.add_argument("--task", required=True)
    s.add_argument("--into", help="expected checked-out target branch")
    s.add_argument("--remove-worktree", action="store_true",
                   help="delete the worker worktree (default: keep it in Done with its terminals closed)")

    s = sub.add_parser("accept", help="finish an approved report task (needs a human approval)")
    s.add_argument("--task", required=True)
    s.add_argument("--remove-worktree", action="store_true",
                   help="delete the worker worktree (default: keep it in Done with its terminals closed)")
    s.add_argument("--export-dir", help=f"where the accepted report is written (default {REPORTS_DIR})")

    s = sub.add_parser("capture", help="log DECISION: lines; close, or list SOT( ) updates still due")
    s.add_argument("--task", required=True)
    s.add_argument("--report", help="worker report path, or - for stdin (default: worker_done report)")
    s.add_argument("--file", default="decisions.md", help="existing Markdown file in the project context dir")
    s.add_argument("--summary", help="override the run_completed summary")
    s.add_argument("--gbrain", action="store_true", help="also `gbrain capture` each decision")

    s = sub.add_parser("sot-done", help="record the updated live SoT documents and close the task")
    s.add_argument("--task", required=True)
    s.add_argument("--file", action="append", required=True,
                   help="updated SoT document (path inside the project folder); repeat per document")

    s = sub.add_parser("gate", help="resolve a non-merge decision gate with the human's answer")
    s.add_argument("--id", required=True)
    s.add_argument("--resolution", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--answer", required=True, help="the human's verbatim answer, for the audit trail")

    s = sub.add_parser("answer", help="answer a pending worker question")
    s.add_argument("--question", required=True)
    s.add_argument("--body", required=True)

    sub.add_parser("recover", help="reconcile attempts with expired leases")
    s = sub.add_parser("status", help="attention queue")
    s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    s = sub.add_parser("reload", help="recover, then print the control-plane snapshot")
    s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    s.add_argument("--limit", type=int, default=5)
    return p


COMMANDS = {
    "project-add": cmd_project_add, "track": cmd_track, "attempt": cmd_attempt, "ingest": cmd_ingest,
    "start-worker": cmd_start_worker, "wait": cmd_wait, "import-tickets": cmd_import_tickets,
    "triage-here": cmd_triage_here,
    "verify": cmd_verify, "review": cmd_review, "approve": cmd_approve, "reject": cmd_reject,
    "merge": cmd_merge, "accept": cmd_accept, "capture": cmd_capture, "sot-done": cmd_sot_done,
    "answer": cmd_answer, "gate": cmd_gate, "discard": cmd_discard,
    "recover": cmd_recover, "status": cmd_status, "reload": cmd_reload,
}


def main(argv: list[str] | None = None) -> int:
    global _NOW
    args = build_parser().parse_args(argv)
    if args.now:
        _NOW = parse_time(args.now)
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        out = COMMANDS[args.command](db, args)
        db.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        if db.in_transaction:
            db.execute("ROLLBACK")
        print(f"busy: another orca-lifecycle command holds the registry lock ({exc}); run it again", file=sys.stderr)
        return 1
    except Refused as exc:
        if db.in_transaction:
            db.execute("ROLLBACK")
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except OrcaError as exc:
        if db.in_transaction:
            db.execute("ROLLBACK")
        print(f"orca error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False) if args.json else render(args.command, out))
    return int(out.get("exit_code", 0)) if isinstance(out, dict) else 0


if __name__ == "__main__":
    sys.exit(main())
