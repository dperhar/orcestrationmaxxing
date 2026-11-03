"""Behavior tests for `bin/orca-lifecycle.py`.

The registry sits beside Orca orchestration. A stub `orca` executable stands in
for the Orca runtime, so these tests prove the registry state machine, not Orca.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin" / "orca-lifecycle.py"

STUB = textwrap.dedent(
    """
    import json, os, sys
    state_path = os.environ["ORCA_STUB_STATE"]
    state = json.load(open(state_path))
    args = [a for a in sys.argv[1:] if a != "--json"]
    state.setdefault("calls", []).append(args)
    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    verb = " ".join(args[:2])
    result = {}
    if verb == "orchestration task-list":
        result = {"tasks": state.get("tasks", [])}
    elif verb == "orchestration task-create":
        n = len(state.setdefault("created", [])) + 1
        task = {"id": f"task_new{n}", "task_title": opt("--task-title"), "deps": opt("--deps"),
                "spec": opt("--spec"), "status": "ready"}
        state["created"].append(task)
        state.setdefault("tasks", []).append(task)
        result = {"task": task}
    elif verb == "worktree list":
        result = {"worktrees": state.get("orca_worktrees", [])}
    elif verb == "worktree create":
        result = {"worktree": {"id": "repo::" + os.environ["ORCA_STUB_WORKTREE"]}}
    elif verb == "terminal create":
        result = {"terminal": {"handle": "term_stub"}}
    elif verb == "terminal read":
        # Orca pastes the brief into the adopted TUI; the stub shows it still waiting in the input box.
        lead = "Please carry out this task from my Orca coordinator by following the brief I pasted below."
        result = {"terminal": {"tail": ["   TODO", "╰─ " + lead + " #1 ─╯"] if state.get("brief_pending") else
                                       ["   ⎋ Reading files", "╰─                                ─╯"]}}
    elif verb == "orchestration worker-start":
        task = next(t for t in state["tasks"] if t["id"] == opt("--task"))
        retry = opt("--retry-of")
        allowed = task.get("status") in (("failed", "blocked") if retry else ("ready",))
        if not allowed or not (opt("--name") or opt("--terminal")) or state.get("fail_worker_start"):
            print(json.dumps({"ok": False, "error": {"code": "invalid_argument"},
                              "result": {"failedStage": "accepted", "residualResources": []}}))
            sys.exit(1)
        n = len(state.setdefault("dispatches", [])) + 1
        state["dispatches"].append(opt("--task"))
        task["status"] = "dispatched"
        result = {"dispatchId": f"ctx_new{n}", "state": "ready", "effects": [
            {"kind": "worktree", "action": "created_child", "id": "repo::" + os.environ["ORCA_STUB_WORKTREE"]}]}
    elif verb == "orchestration check":
        if "--ack" in args:
            state.setdefault("acked", []).append(opt("--ack"))
        inbox = state.setdefault("inbox", [])
        result = inbox.pop(0) if inbox else {"messages": [], "count": 0, "deliveryId": None}
    elif verb == "orchestration task-update" and opt("--id") in state.get("fail_task_update", []):
        json.dump(state, open(state_path, "w"))
        print(json.dumps({"ok": False, "error": {"code": "conflict"}}))
        sys.exit(1)
    elif verb == "orchestration task-update":
        for t in state.get("tasks", []):
            if t["id"] == opt("--id"):
                t["status"] = opt("--status")
    elif verb == "orchestration gate-create":
        gate = {"id": "gate_1", "task_id": opt("--task"), "status": "pending", "resolution": None}
        state.setdefault("gates", []).append(gate)
        result = {"gate": gate}
    elif verb == "orchestration gate-resolve":
        for g in state.get("gates", []):
            if g["id"] == opt("--id"):
                g.update(status="resolved", resolution=opt("--resolution"))
    elif verb == "orchestration worker-list":
        if state.get("fail_worker_list"):
            print(json.dumps({"ok": False, "error": {"code": "runtime_unreachable"}}))
            sys.exit(1)
        result = {"workers": state.get("workers", []), "page": {}}
    elif verb == "orchestration worker-stop" and (state.get("fail_worker_stop") is True
                                                   or opt("--dispatch") in (state.get("fail_worker_stop") or [])):
        json.dump(state, open(state_path, "w"))
        print(json.dumps({"ok": False, "error": {"code": "stop_unconfirmed"}}))
        sys.exit(1)
    elif verb == "orchestration gate-list":
        result = {"gates": [g for g in state.get("gates", [])
                            if (not opt("--status") or g["status"] == opt("--status"))
                            and (not opt("--task") or g["task_id"] == opt("--task"))]}
    json.dump(state, open(state_path, "w"))
    print(json.dumps({"ok": True, "result": result}))
    """
)


@pytest.fixture
def env(tmp_path):
    stub = tmp_path / "orca_stub.py"
    stub.write_text(STUB)
    state = tmp_path / "orca_state.json"
    state.write_text(json.dumps({"tasks": [{"id": "task_a", "task_title": "Add greeting"}]}))
    e = dict(os.environ)
    e.update({
        "ORCA_LIFECYCLE_DB": str(tmp_path / "lifecycle.db"),
        "ORCA_CLI_COMMAND": f"{sys.executable} {stub}",
        "ORCA_STUB_STATE": str(state),
        "ORCA_ORCHESTRATION_DB": str(tmp_path / "missing-orca.db"),
        "ORCA_STUB_WORKTREE": str(tmp_path / "wt"),
        "ORCA_LIFECYCLE_OMP_OVERLAY": str(tmp_path / "noplan.yml"),
        "ORCA_OMP_FRESH_CONFIG": str(tmp_path / "fresh.yml"),
        "ORCA_OMP_STATUS_EXTENSION": str(tmp_path / "status.ts"),
        "ORCA_TRIAGE_CONFIG": str(tmp_path / "orca-triage.json"),
        "ORCA_REPORTS_DIR": str(tmp_path / "reports"),
        "ORCA_USER_DATA_PATH": str(tmp_path / "orca-user-data"),
        "ORCA_WORKTREE_ID": "",  # the triage chat's Orca worktree; `project` points it at the project repo
    })
    return {"env": e, "tmp": tmp_path, "state": state}


def run(env, *args, stdin=None, check=True, cwd=None):
    proc = subprocess.run([sys.executable, str(CLI), *args], input=stdin, capture_output=True,
                          text=True, env=env["env"], cwd=cwd)
    if check and proc.returncode != 0:
        raise AssertionError(f"exit {proc.returncode}: {proc.stderr}")
    return proc


def jrun(env, *args, stdin=None):
    return json.loads(run(env, *args, stdin=stdin).stdout)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def project(env):
    """A real git repo with a feature branch in its own worktree."""
    repo = env["tmp"] / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "decisions.md").write_text("# Decisions\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    wt = env["tmp"] / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feat/greeting", str(wt))
    (wt / "greet.txt").write_text("hello\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "greeting")
    run(env, "project-add", "--repo-path", str(repo), "--slug", "acme/demo")
    run(env, "track", "--task", "task_a", "--run", "run_1", "--project", "acme/demo",
        "--acceptance", "greet.txt says hello")
    run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_1", "--worktree-path", str(wt))
    env["env"]["ORCA_WORKTREE_ID"] = f"repo::{repo}"
    return {"repo": repo, "wt": wt}


def worker_done(body="Added greet.txt. It prints hello. Nothing is left.", outcome="succeeded",
                dispatch="ctx_1"):
    payload = json.dumps({"taskId": "task_a", "dispatchId": dispatch, "outcome": outcome})
    return {"id": f"msg_{abs(hash(body)) % 10**6}", "type": "worker_done", "subject": "done",
            "body": body, "payload": payload}


def batch(*messages, delivery="dlv_1"):
    return json.dumps({"ok": True, "result": {"delivery": {"id": delivery}, "messages": list(messages)}})


def events(env, transition):
    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    return db.execute("SELECT entity_id, evidence_json FROM events WHERE transition=?", (transition,)).fetchall()


def stage(env, task="task_a"):
    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    return db.execute("SELECT stage FROM tasks WHERE task_id=?", (task,)).fetchone()[0]


def test_duplicate_worker_done_settles_once(env, project):
    first = jrun(env, "ingest", stdin=batch(worker_done(), worker_done()))
    again = jrun(env, "ingest", stdin=batch(worker_done(), delivery="dlv_2"))

    assert [p["stage"] for p in first["processed"]] == ["verifying"]
    assert first["duplicates"] == 1 and again["duplicates"] == 1
    assert first["ack"] == ["orca", "orchestration", "check", "--ack", "dlv_1"]
    assert len(events(env, "task_completed")) == 1
    assert stage(env) == "verifying"


def test_conflicting_worker_done_is_reconciled_not_applied(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    late = jrun(env, "ingest", stdin=batch(worker_done(body="It failed after all.", outcome="failed"),
                                           delivery="dlv_2"))

    assert stage(env) == "verifying"
    assert late["ack"] == ["orca", "orchestration", "check", "--ack", "dlv_2"]
    assert late["processed"] == [] and late["duplicates"] == 1
    assert len(events(env, "duplicate_event_mismatch")) == 1
    assert len(events(env, "task_completed")) == 1


def test_out_of_stage_completion_applies_after_stage_is_corrected(env, project):
    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    db.execute("UPDATE tasks SET stage='reviewing' WHERE task_id='task_a'")
    db.commit()
    blocked = jrun(env, "ingest", stdin=batch(worker_done()))
    db.execute("UPDATE tasks SET stage='implementing' WHERE task_id='task_a'")
    db.commit()
    replay = jrun(env, "ingest", stdin=batch(worker_done()))

    assert "ack" not in blocked and len(blocked["errors"]) == 1
    assert [p["stage"] for p in replay["processed"]] == ["verifying"] and "ack" in replay
    assert len(events(env, "task_completed")) == 1


def test_untracked_or_miscorrelated_completion_withholds_ack(env, project):
    stranger = worker_done(dispatch="ctx_unknown")
    wrong_task = {**worker_done(), "id": "msg_wrong",
                  "payload": json.dumps({"taskId": "task_other", "dispatchId": "ctx_1", "outcome": "succeeded"})}
    out = jrun(env, "ingest", stdin=batch(stranger, wrong_task))

    assert out["processed"] == [] and len(out["errors"]) == 2
    assert "ack" not in out
    assert stage(env) == "implementing" and events(env, "task_completed") == []


def test_malformed_payload_withholds_ack_and_keeps_good_messages(env, project):
    broken = {"id": "msg_bad", "type": "worker_done", "subject": "x", "body": "", "payload": "{not json"}
    no_dispatch = {"id": "msg_nod", "type": "worker_done", "subject": "x", "body": "",
                   "payload": json.dumps({"taskId": "task_a", "outcome": "succeeded"})}
    out = jrun(env, "ingest", stdin=batch(broken, no_dispatch, worker_done()))

    assert [p["stage"] for p in out["processed"]] == ["verifying"]
    assert [e["message_id"] for e in out["errors"]] == ["msg_bad", "msg_nod"]
    assert "ack" not in out and "dlv_1" in out["ack_withheld"]


def test_worker_done_without_task_id_settles_through_its_dispatch(env, project):
    no_task = {"id": "msg_notask", "type": "worker_done", "subject": "done", "body": "Done. Nothing is left.",
               "payload": json.dumps({"dispatchId": "ctx_1", "outcome": "succeeded"})}
    out = jrun(env, "ingest", stdin=batch(no_task))
    assert out["errors"] == [] and out["ack"] == ["orca", "orchestration", "check", "--ack", "dlv_1"]
    assert [(p["task_id"], p["stage"]) for p in out["processed"]] == [("task_a", "verifying")]


def test_discarded_message_lets_the_same_batch_be_acked(env, project):
    stray = {"id": "msg_stray", "type": "worker_done", "subject": "x", "body": "",
             "payload": json.dumps({"dispatchId": "ctx_unknown", "outcome": "succeeded"})}
    first = jrun(env, "ingest", stdin=batch(stray, worker_done()))
    assert "ack" not in first and [e["message_id"] for e in first["errors"]] == ["msg_stray"]
    assert run(env, "discard", "--message", "msg_stray", check=False).returncode == 2  # a reason is required

    jrun(env, "discard", "--message", "msg_stray", "--reason", "dispatch from a deleted run")
    again = jrun(env, "ingest", stdin=batch(stray, worker_done()))

    assert again["errors"] == [] and again["discarded"] == ["msg_stray"] and again["duplicates"] == 1
    assert again["ack"] == ["orca", "orchestration", "check", "--ack", "dlv_1"]
    ((_, evidence),) = events(env, "discarded")
    assert json.loads(evidence)["reason"] == "dispatch from a deleted run"


def test_failed_verification_routes_to_improve_and_blocks_merge(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    out = jrun(env, "verify", "--task", "task_a", "--verdict", "fail", "--evidence", "pytest: 1 failed")

    assert out["stage"] == "rework" and "improve" in out["next"]
    refused = run(env, "merge", "--task", "task_a", check=False)
    assert refused.returncode == 2 and "human approval" in refused.stderr
    assert "feat/greeting" not in git(project["repo"], "branch", "--merged", "main")


def test_negative_board_verdict_blocks_approval(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "pytest: 3 passed")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    out = jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "fail", "--evidence", "scope")

    assert out["stage"] == "rework" and "improve" in out["next"]
    assert run(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve",
               check=False).returncode == 2


def test_merge_needs_explicit_human_approval(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "pytest: 3 passed")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    gated = jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "pass", "--evidence", "ok")
    assert gated["stage"] == "awaiting_approval"
    assert run(env, "merge", "--task", "task_a", check=False).returncode == 2

    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    merged = jrun(env, "merge", "--task", "task_a", "--into", "main")

    assert merged["stage"] == "merged"
    assert git(project["repo"], "log", "-1", "--format=%P", merged["sha"]).count(" ") == 1  # merge commit
    assert (project["repo"] / "greet.txt").read_text() == "hello\n"
    calls = json.loads(env["state"].read_text())["calls"]
    tip = git(project["repo"], "rev-parse", "feat/greeting").strip()
    assert ["orchestration", "gate-resolve", "--id", "gate_1", "--resolution",
            f"approve (by owner) at {tip[:12]}"] in calls


def _board(env, wt):
    """Board statuses the lifecycle set on one worktree, in order."""
    calls = json.loads(env["state"].read_text())["calls"]
    return [c[c.index("--workspace-status") + 1] for c in calls
            if c[:2] == ["worktree", "set"] and c[c.index("--worktree") + 1] == f"path:{wt}"
            and "--workspace-status" in c]


def _board_columns(env, *columns):
    data = Path(env["env"]["ORCA_USER_DATA_PATH"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "orca-data.json").write_text(json.dumps(
        {"ui": {"workspaceStatuses": [{"id": i, "label": label} for i, label in columns]}}))


def test_board_status_follows_the_lifecycle_and_merge_switches_the_worker_off(env, project):
    _merge(env)

    assert _board(env, project["wt"]) == ["in-progress", "in-review", "completed"]
    calls = json.loads(env["state"].read_text())["calls"]
    assert ["terminal", "close", "--worktree", f"path:{project['wt']}", "--all"] in calls
    assert not [c for c in calls if c[:2] == ["worktree", "rm"]]
    assert project["wt"].is_dir()


def test_merge_removes_the_worktree_only_when_asked(env, project):
    _merge_with(env, "--remove-worktree")
    calls = json.loads(env["state"].read_text())["calls"]
    assert ["worktree", "rm", "--worktree", f"path:{project['wt']}"] in calls


def test_merge_after_rework_finishes_the_worktrees_of_every_attempt(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "fail", "--evidence", "pytest: 1 failed")
    wt2 = env["tmp"] / "wt2"
    git(project["repo"], "worktree", "add", "-q", "-b", "feat/greeting-2", str(wt2), "feat/greeting")
    (wt2 / "greet.txt").write_text("hello again\n")
    git(wt2, "commit", "-qam", "fix greeting")
    run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--worktree-path", str(wt2))
    jrun(env, "ingest", stdin=batch(worker_done(body="Fixed greet.txt. Nothing is left.", dispatch="ctx_2"),
                                    delivery="dlv_2"))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")

    jrun(env, "merge", "--task", "task_a")

    assert _board(env, project["wt"])[-1] == "completed" and _board(env, wt2)[-1] == "completed"
    calls = json.loads(env["state"].read_text())["calls"]
    for wt in (project["wt"], wt2):
        assert ["terminal", "close", "--worktree", f"path:{wt}", "--all"] in calls


@pytest.mark.parametrize("columns, fix", [
    ([("in-progress", "In progress"), ("status-5", "To Fix"), ("in-review", "In review")], "status-5"),
    (None, "in-progress"),  # no board file, or no "To Fix" column
])
def test_failed_verification_moves_the_worktree_to_to_fix(env, project, columns, fix):
    if columns:
        _board_columns(env, *columns)
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "fail", "--evidence", "pytest: 1 failed")
    assert _board(env, project["wt"]) == ["in-progress", "in-review", fix]


def _to_approval(env):
    jrun(env, "ingest", stdin=batch(worker_done(
        body="Added greet.txt. It prints hello. Nothing is left.\nDECISION: Greetings stay in plain text files.")))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "pass", "--evidence", "ok")


def _merge(env):
    _merge_with(env)


def _merge_with(env, *flags):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    jrun(env, "merge", "--task", "task_a", *flags)


def test_capture_appends_only_durable_decisions_and_reload_uses_summary(env, project):
    _merge(env)
    report = env["tmp"] / "report.md"
    report.write_text("Renamed a local variable in greet.py.\nDECISION: Greetings stay in plain text files.\n")
    out = jrun(env, "capture", "--task", "task_a", "--report", str(report))

    text = (project["repo"] / "decisions.md").read_text()
    assert out["decisions_appended"] == 1
    assert text.count("Greetings stay in plain text files.") == 1
    assert "Renamed a local variable" not in text
    snapshot = jrun(env, "reload", "--json")
    summary = snapshot["recent_completions"][0]["summary"]
    assert summary.startswith("Added greet.txt. It prints hello. Nothing is left. Merged feat/greeting into main at ")
    assert stage(env) == "closed"


def test_capture_creates_only_the_decisions_log(env, project):
    _merge(env)
    refused = run(env, "capture", "--task", "task_a", "--file", "product.md", check=False)
    assert refused.returncode == 2 and "only the operational log" in refused.stderr
    assert not (project["repo"] / "product.md").exists()
    assert stage(env) == "merged"

    (project["repo"] / "decisions.md").unlink()
    git(project["repo"], "commit", "-qam", "drop log")
    jrun(env, "capture", "--task", "task_a")
    assert "Greetings stay in plain text files." in (project["repo"] / "decisions.md").read_text()


def _merge_with_sot(env):
    jrun(env, "ingest", stdin=batch(worker_done(body=(
        "Added greet.txt. Done. Nothing left.\nDECISION: Greetings stay in plain text files.\n"
        "SOT(architecture): Greetings are served from greet.txt.\nSOT(product): Users see a greeting."))))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    jrun(env, "merge", "--task", "task_a")


def test_sot_changes_keep_the_task_open_until_the_live_documents_are_updated(env, project):
    repo = project["repo"]
    (repo / "ARCHITECTURE.md").write_text("# Architecture\nOld.\n")
    (repo / "Product").mkdir()
    (repo / "Product" / "overview.md").write_text("# Product\nOld.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "sot docs")
    git(project["wt"], "merge", "-q", "--no-edit", "main")  # the worker branch is current with main
    _merge_with_sot(env)

    out = jrun(env, "--now", "2100-01-01T00:00:00Z", "capture", "--task", "task_a")
    assert out["stage"] == "merged" and {p["area"] for p in out["sot_pending"]} == {"architecture", "product"}
    assert stage(env) == "merged"
    (row,) = jrun(env, "status", "--json")["rows"]
    assert row["priority"] == 2 and "sot-done --task task_a" in row["next"]

    stale = run(env, "sot-done", "--task", "task_a", "--file", "ARCHITECTURE.md", "--file", "Product/overview.md",
                check=False)
    assert stale.returncode == 2 and "was not changed after capture" in stale.stderr

    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    db.execute("UPDATE events SET timestamp='2000-01-01T00:00:00Z' WHERE transition='captured'")  # edits come after
    db.commit()
    (repo / "ARCHITECTURE.md").write_text("# Architecture\nGreetings are served from greet.txt.\n")
    partial = run(env, "sot-done", "--task", "task_a", "--file", "ARCHITECTURE.md", check=False)
    assert partial.returncode == 2 and "product" in partial.stderr

    (repo / "Product" / "overview.md").write_text("# Product\nUsers see a greeting.\n")
    done = jrun(env, "sot-done", "--task", "task_a", "--file", "ARCHITECTURE.md", "--file", "Product/overview.md")

    assert done["stage"] == "closed" and stage(env) == "closed"
    assert git(repo, "status", "--porcelain").strip() == ""  # tracked SoT docs and the log are committed
    assert "docs(sot): task_a architecture, product" in git(repo, "log", "-3", "--format=%s")


def test_capture_refuses_files_outside_the_context_folder(env, project):
    _merge(env)
    outside = env["tmp"] / "outside.md"
    outside.write_text("# Not project context\n")
    refused = run(env, "capture", "--task", "task_a", "--file", "../outside.md", check=False)

    assert refused.returncode == 2 and "outside the project context folder" in refused.stderr
    assert outside.read_text() == "# Not project context\n"


def _add_commit(wt, name):
    (wt / name).write_text("unreviewed\n")
    git(wt, "add", name)
    git(wt, "commit", "-qm", f"add {name}")


def test_commit_after_verification_sends_task_back_to_verify(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")
    _add_commit(project["wt"], "sneaky.txt")

    out = jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    assert out["stage"] == "verifying" and "verify again" in out["next"]


def test_commit_after_approval_blocks_merge(env, project):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    _add_commit(project["wt"], "sneaky.txt")

    refused = run(env, "merge", "--task", "task_a", check=False)
    assert refused.returncode == 2 and "no review or approval" in refused.stderr
    assert not (project["repo"] / "greet.txt").exists()


def test_approve_retry_after_gate_already_resolved(env, project):
    _to_approval(env)
    state = json.loads(env["state"].read_text())
    state["gates"][0].update(status="resolved", resolution="approve (by owner)")  # crash after gate-resolve
    env["state"].write_text(json.dumps(state))

    assert jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")["stage"] == "approved"
    calls = json.loads(env["state"].read_text())["calls"]
    assert not any(c[:2] == ["orchestration", "gate-resolve"] for c in calls)


def test_merge_resumes_after_crash_without_second_merge(env, project):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    git(project["repo"], "merge", "--no-ff", "-m", "merge(task_a): Add greeting", "feat/greeting")
    crashed_sha = git(project["repo"], "rev-parse", "HEAD").strip()

    out = jrun(env, "merge", "--task", "task_a")

    assert out["sha"] == crashed_sha
    assert git(project["repo"], "rev-parse", "HEAD").strip() == crashed_sha


def test_merge_resumes_after_orca_deleted_the_branch(env, project):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    git(project["repo"], "merge", "--no-ff", "-m", "merge(task_a): Add greeting", "feat/greeting")
    crashed_sha = git(project["repo"], "rev-parse", "HEAD").strip()
    git(project["repo"], "worktree", "remove", "--force", str(project["wt"]))
    git(project["repo"], "branch", "-d", "feat/greeting")

    assert jrun(env, "merge", "--task", "task_a")["sha"] == crashed_sha


def test_merge_resumes_when_target_moved_after_crash(env, project):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    git(project["repo"], "merge", "--no-ff", "-m", "merge(task_a): Add greeting", "feat/greeting")
    crashed_sha = git(project["repo"], "rev-parse", "HEAD").strip()
    (project["repo"] / "other.txt").write_text("x\n")
    git(project["repo"], "add", "other.txt")
    git(project["repo"], "commit", "-qm", "unrelated commit lands before the retry")

    assert jrun(env, "merge", "--task", "task_a")["sha"] == crashed_sha


def test_merge_keeps_unrelated_owner_changes_and_refuses_on_overlap(env, project):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    git(project["repo"], "rm", "-q", "--cached", "decisions.md")
    git(project["repo"], "commit", "-qm", "untrack decisions")
    (project["repo"] / "notes.md").write_text("owner\n")
    git(project["repo"], "add", "notes.md")
    git(project["repo"], "commit", "-qm", "notes")
    (project["repo"] / "notes.md").write_text("owner work in progress\n")  # tracked, not in the branch diff
    (project["repo"] / "greet.txt").write_text("owner draft\n")  # untracked, but the branch adds it

    refused = run(env, "merge", "--task", "task_a", check=False)
    assert refused.returncode == 2 and "greet.txt" in refused.stderr
    assert (project["repo"] / "greet.txt").read_text() == "owner draft\n" and stage(env) == "approved"

    (project["repo"] / "greet.txt").unlink()
    merged = jrun(env, "merge", "--task", "task_a")

    assert merged["stage"] == "merged" and merged["kept_dirty"] == ["notes.md"]
    assert (project["repo"] / "notes.md").read_text() == "owner work in progress\n"
    assert (project["repo"] / "greet.txt").read_text() == "hello\n"


def test_merge_refuses_branch_without_new_commits(env, project):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    git(project["repo"], "merge", "--ff-only", "feat/greeting")  # target already contains the work

    refused = run(env, "merge", "--task", "task_a", check=False)
    assert refused.returncode == 2 and "no commits" in refused.stderr
    assert stage(env) == "approved"


def test_merge_conflict_after_approval_can_be_rejected_and_reapproved(env, project):
    _to_approval(env)
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    (project["repo"] / "greet.txt").write_text("conflicting line\n")
    git(project["repo"], "add", "greet.txt")
    git(project["repo"], "commit", "-qm", "another task touched greet.txt first")

    conflict = run(env, "merge", "--task", "task_a", check=False)
    assert conflict.returncode == 2 and "reject --task task_a" in conflict.stderr
    out = jrun(env, "reject", "--task", "task_a", "--by", "owner", "--answer", "Reject", "--reason", "conflict")
    assert out["stage"] == "rework"

    # The retry runs the full cycle again; the second approval must not collide with the first.
    git(project["wt"], "merge", "-q", "-X", "theirs", "main", "-m", "take the target's greet.txt")
    run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--worktree-path", str(project["wt"]))
    jrun(env, "ingest", stdin=batch(worker_done(body="Rebased. Merges clean. Nothing left.", dispatch="ctx_2")))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "pass", "--evidence", "ok")
    assert jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")["stage"] == "approved"
    assert jrun(env, "merge", "--task", "task_a")["stage"] == "merged"


def test_capture_retry_does_not_duplicate_decisions(env, project):
    _merge(env)
    with (project["repo"] / "decisions.md").open("a") as fh:  # crash after the append, before commit
        fh.write("- 2026-10-18 · Add greeting (task_a): Greetings stay in plain text files.\n")
    jrun(env, "capture", "--task", "task_a")

    assert (project["repo"] / "decisions.md").read_text().count("Greetings stay in plain text files.") == 1
    assert stage(env) == "closed"


def _set_workers(env, *workers):
    state = json.loads(env["state"].read_text())
    state["workers"] = list(workers)
    env["state"].write_text(json.dumps(state))


def _worker(dispatch, verdict, status="dispatched"):
    return {"dispatchId": dispatch, "dispatchStatus": status,
            "projection": {"liveness": {"verdict": verdict}, "attention": {"requiresAction": True}}}


def test_recovery_abandons_only_on_positive_exit_proof(env, project):
    _set_workers(env, _worker("ctx_1", "unverifiable"))
    stale = jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")
    assert stale["decisions"][0]["verdict"] == "stale_unverified" and stage(env) == "implementing"

    _set_workers(env, _worker("ctx_1", "exited", "failed"))
    out = jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")
    assert out["decisions"][0]["verdict"] == "abandoned"
    (dispatch, evidence), = events(env, "crash_recovery_abandoned")
    assert json.loads(evidence)["branch_preserved"] == "feat/greeting"
    assert "feat/greeting" in git(project["repo"], "branch", "--list", "feat/greeting")
    assert stage(env) == "abandoned"


def test_late_completion_from_superseded_attempt_never_settles(env, project):
    _set_workers(env, _worker("ctx_1", "exited", "failed"))
    jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")
    run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--worktree-path", str(project["wt"]))

    late = jrun(env, "ingest", stdin=batch(worker_done(dispatch="ctx_1")))
    assert late["superseded"] == 1 and late["processed"] == [] and "ack" in late
    assert stage(env) == "implementing"

    current = jrun(env, "ingest", stdin=batch(worker_done(body="Retry done. All good. Nothing left.",
                                                          dispatch="ctx_2"), delivery="dlv_2"))
    assert [p["stage"] for p in current["processed"]] == ["verifying"]


@pytest.mark.parametrize("ending", ["failed", "abandoned"])
def test_ended_task_stays_in_queue_until_cancelled_in_orca(env, project, ending):
    if ending == "failed":
        git(project["repo"], "worktree", "remove", "--force", str(project["wt"]))
        git(project["repo"], "branch", "-D", "feat/greeting")
        _set_workers(env, _worker("ctx_1", "unverifiable"))
    else:
        _set_workers(env, _worker("ctx_1", "exited", "failed"))
    jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")

    rows = jrun(env, "status", "--json")["rows"]
    assert [(r["task_id"], r["stage"]) for r in rows] == [("task_a", ending)]
    assert "task-update --id task_a --run run_1 --status failed" in rows[0]["next"]
    state = json.loads(env["state"].read_text())
    state["tasks"][0]["status"] = "failed"  # Orca failed the Task on its own: still a user decision
    env["state"].write_text(json.dumps(state))
    assert len(jrun(env, "status", "--json")["rows"]) == 1
    state["tasks"][0]["result"] = "cancelled"  # user cancelled the Task in Orca
    env["state"].write_text(json.dumps(state))
    assert jrun(env, "status", "--json")["rows"] == []


def _orca_status(env, task, status):
    state = json.loads(env["state"].read_text())
    next(t for t in state["tasks"] if t["id"] == task)["status"] = status
    env["state"].write_text(json.dumps(state))


def _calls(env, verb):
    return [c for c in json.loads(env["state"].read_text()).get("calls", []) if c[:2] == ["orchestration", verb]]


@pytest.mark.parametrize("orca_status, reopened, retry_of", [
    ("completed", True, None),
    ("failed", False, "ctx_1"),
    ("ready", False, None),
])
def test_start_worker_picks_the_start_orca_accepts(env, project, orca_status, reopened, retry_of):
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "fail", "--evidence", "pytest: 1 failed")
    _orca_status(env, "task_a", orca_status)

    out = jrun(env, "start-worker", "--task", "task_a", "--class", "small", "--agent", "claude")

    (start,) = _calls(env, "worker-start")
    assert bool(_calls(env, "task-update")) is reopened
    assert ("--retry-of" in start) is bool(retry_of)
    if retry_of:
        assert start[start.index("--retry-of") + 1] == retry_of
    repo = git(project["repo"], "worktree", "list", "--porcelain").split("\n", 1)[0].removeprefix("worktree ")
    for flag, value in (("--repo", f"path:{repo}"), ("--worktree", "new-child"), ("--base-branch", "main"),
                        ("--name", "add-greeting-task_a-a2"), ("--run", "run_1")):
        assert start[start.index(flag) + 1] == value
    assert out["dispatch_id"] == "ctx_new1" and out["branch"] == "feat/greeting"
    assert stage(env) == "implementing"
    # agents other than OMP keep the one-call agent-first start: no adopted terminal, no low-level launch calls
    assert "--terminal" not in start and start[start.index("--agent") + 1] == "claude"
    calls = json.loads(env["state"].read_text())["calls"]
    assert not [c for c in calls if c[0] in ("worktree", "terminal") and c[:2] not in (["worktree", "set"], ["worktree", "list"])]
    assert '"class":"small"' in dict(events(env, "started"))["ctx_new1"]
    assert _board(env, project["wt"])[-1] == "in-progress"


def test_start_worker_refuses_a_chat_outside_the_project_repo(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "fail", "--evidence", "pytest: 1 failed")
    _orca_status(env, "task_a", "ready")
    env["env"]["ORCA_WORKTREE_ID"] = f"other::{env['tmp']}"  # chat elsewhere; the shell cwd is inside the repo
    proc = run(env, "start-worker", "--task", "task_a", "--class", "small", "--agent", "omp", check=False,
               cwd=project["repo"])
    assert proc.returncode == 2 and "child worktree" in proc.stderr
    assert _calls(env, "worker-start") == [] and _calls(env, "task-create") == []
    proc = run(env, "start-worker", "--spec", "x", "--title", "T", "--project", "acme/demo", "--run", "run_1",
               "--acceptance", "a", "--agent", "omp", "--class", "small", check=False)
    assert proc.returncode == 2 and _calls(env, "task-create") == []


def test_start_worker_requires_class(env, project):
    _orca_status(env, "task_a", "ready")
    proc = run(env, "start-worker", "--task", "task_a", "--agent", "omp", check=False)
    assert proc.returncode == 2 and "--class" in proc.stderr
    assert _calls(env, "worker-start") == []


def _ready_for_restart(env):
    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "fail", "--evidence", "pytest: 1 failed")
    _orca_status(env, "task_a", "ready")


@pytest.mark.parametrize("work_class", ["small", "large"])
def test_omp_worker_never_starts_in_plan_mode(env, project, work_class):
    _ready_for_restart(env)

    out = jrun(env, "start-worker", "--task", "task_a", "--class", work_class, "--agent", "omp")

    calls = json.loads(env["state"].read_text())["calls"]
    launch = [c for c in calls if (c[0] in ("worktree", "terminal") and c[:2] not in (["worktree", "set"], ["worktree", "list"]))
              or c[:2] == ["orchestration", "worker-start"]]
    assert [c[:2] for c in launch] == [["worktree", "create"], ["terminal", "create"], ["terminal", "wait"],
                                       ["orchestration", "worker-start"], ["terminal", "wait"],
                                       ["terminal", "read"]]
    command = launch[1][launch[1].index("--command") + 1]
    overlay = env["env"]["ORCA_LIFECYCLE_OMP_OVERLAY"]
    assert command.startswith("omp ") and overlay in command and env["env"]["ORCA_OMP_FRESH_CONFIG"] in command
    # the overlay turns plan mode and every advisor off and leaves compaction to the session defaults
    assert Path(overlay).read_text() == (
        "plan:\n  defaultOnStartup: false\nadvisor:\n  enabled: false\n"
        "task:\n  agentAdvisor:\n    code-implementer: \"off\"\n    worker-advisor: \"off\"\n")
    start = launch[3]
    assert start[start.index("--terminal") + 1] == "term_stub"
    assert start[start.index("--worktree") + 1] == f"id:repo::{project['wt']}"
    assert not {"--agent", "--name", "--model", "--repo"} & set(start)
    assert out["dispatch_id"] == "ctx_new1" and out["worktree_path"] == str(project["wt"])
    assert f'"class":"{work_class}"' in dict(events(env, "started"))["ctx_new1"]


def test_omp_worker_brief_left_in_the_input_box_is_submitted(env, project):
    _ready_for_restart(env)
    state = json.loads(env["state"].read_text())
    state["brief_pending"] = True
    env["state"].write_text(json.dumps(state))

    jrun(env, "start-worker", "--task", "task_a", "--class", "small", "--agent", "omp")

    calls = json.loads(env["state"].read_text())["calls"]
    assert ["terminal", "send", "--terminal", "term_stub", "--enter"] in calls
    assert dict(events(env, "brief_submitted"))["ctx_new1"]


def test_large_class_with_another_agent_uses_the_plain_start(env, project):
    _ready_for_restart(env)
    jrun(env, "start-worker", "--task", "task_a", "--class", "large", "--agent", "claude")
    (start,) = _calls(env, "worker-start")
    assert start[start.index("--agent") + 1] == "claude" and "--terminal" not in start


def test_start_worker_with_spec_creates_tracks_and_appends_the_contract(env, project):
    out = jrun(env, "start-worker", "--spec", "Write the farewell file.", "--title", "Add farewell",
               "--project", "acme/demo", "--run", "run_1", "--acceptance", "farewell.txt exists",
               "--agent", "omp", "--kind", "report", "--class", "small")

    (create,) = _calls(env, "task-create")
    spec = create[create.index("--spec") + 1]
    assert spec.startswith("Write the farewell file.") and "DECISION: <one short sentence>" in spec
    assert "SOT(architecture|business|dev|product)" in spec and "--report-path" in spec
    for rule in ("out of scope:", "300 changed lines", "no reviewer subagents", "full test suite more than once",
                 "After worker_done, stop working", "Do not enter plan mode", "--task-id", "--dispatch-id",
                 "agent `code-implementer`", "agent `worker-advisor`", "agent `scout`", "agent `sonic`"):
        assert rule in spec
    assert out["task_id"] == "task_new1" and out["dispatch_id"] == "ctx_new1"
    row = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"]).execute(
        "SELECT kind, stage FROM tasks WHERE task_id='task_new1'").fetchone()
    assert row == ("report", "implementing")


def test_failed_worker_start_keeps_the_task_tracked_and_records_no_attempt(env, project):
    state = json.loads(env["state"].read_text())
    state["fail_worker_start"] = True
    env["state"].write_text(json.dumps(state))

    proc = run(env, "start-worker", "--spec", "x", "--title", "T", "--project", "acme/demo", "--run", "run_1",
               "--acceptance", "a", "--agent", "omp", "--class", "small", check=False)

    out = json.loads(proc.stdout)
    assert proc.returncode == 1 and out["failedStage"].endswith("accepted") and "do not relaunch" in out["next"]
    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    assert db.execute("SELECT stage FROM tasks WHERE task_id='task_new1'").fetchone() == ("routed",)
    assert db.execute("SELECT count(*) FROM attempts WHERE task_id='task_new1'").fetchone() == (0,)


def _inbox(env, *batches):
    state = json.loads(env["state"].read_text())
    state["inbox"] = [json.loads(b)["result"] for b in batches]
    env["state"].write_text(json.dumps(state))


def test_wait_records_each_batch_before_acking_it(env, project):
    question = {"id": "msg_q", "type": "question", "subject": "Question", "body": "Tabs or spaces?",
                "payload": json.dumps({"taskId": "task_a", "dispatchId": "ctx_1"})}
    _inbox(env, batch(question, delivery="dlv_1"), batch(worker_done(), delivery="dlv_2"))

    out = jrun(env, "wait", "--timeout-ms", "1000")

    assert out["batches"] == 2 and [q["message_id"] for q in out["questions"]] == ["msg_q"]
    assert json.loads(env["state"].read_text())["acked"] == ["dlv_1", "dlv_2"]
    assert stage(env) == "verifying"


def test_wait_withholds_ack_when_a_batch_has_errors(env, project):
    _inbox(env, batch(worker_done(dispatch="ctx_unknown"), delivery="dlv_1"))

    proc = run(env, "wait", "--timeout-ms", "1000", check=False)

    assert proc.returncode == 1 and "dlv_1" in json.loads(proc.stdout)["ack_withheld"]
    assert "acked" not in json.loads(env["state"].read_text())


def test_report_task_is_pinned_by_hash_accepted_without_merge_and_captured(env, project):
    report = env["tmp"] / "report.md"
    report.write_text("# Findings\nThe API caps at 100 rps.\nDECISION: Batch requests in groups of 50.\n")
    state = json.loads(env["state"].read_text())
    state["tasks"].append({"id": "task_r", "task_title": "Research rate limits"})
    env["state"].write_text(json.dumps(state))
    run(env, "track", "--task", "task_r", "--run", "run_1", "--project", "acme/demo", "--acceptance", "x",
        "--kind", "report")
    run(env, "attempt", "--task", "task_r", "--dispatch", "ctx_r")
    done = {"id": "msg_r", "type": "worker_done", "subject": "done", "body": "Measured it. 100 rps. Done.",
            "payload": json.dumps({"taskId": "task_r", "dispatchId": "ctx_r", "outcome": "succeeded",
                                   "reportPath": str(report)})}
    jrun(env, "ingest", stdin=batch(done))
    pin = jrun(env, "verify", "--task", "task_r", "--verdict", "pass", "--evidence", "read it")["reviewed_sha"]
    jrun(env, "review", "--task", "task_r", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_r", "--kind", "board", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "approve", "--task", "task_r", "--by", "owner", "--answer", "Approve")

    refused = run(env, "merge", "--task", "task_r", check=False)
    assert refused.returncode == 2 and "accept --task task_r" in refused.stderr
    accepted = jrun(env, "accept", "--task", "task_r")
    assert accepted["report_sha"] == pin
    assert Path(accepted["report"]).read_text() == report.read_text()  # the deliverable survives the worktree
    report.unlink()  # capture must use the stored report, not the worker's file
    out = jrun(env, "capture", "--task", "task_r")
    assert out["decisions_appended"] == 1 and out["summary"].endswith(f"Accepted report {pin[:12]}.")
    assert "Batch requests in groups of 50." in (project["repo"] / "decisions.md").read_text()


@pytest.mark.parametrize("extra_file, forced", [(False, True), (True, False)])
def test_accept_forces_worktree_removal_only_when_the_stored_report_is_the_sole_change(
        env, project, extra_file, forced):
    git(project["repo"], "worktree", "add", "-q", "-b", "report/rl", str(env["tmp"] / "rwt"))
    rwt = env["tmp"] / "rwt"
    report = rwt / "report.md"
    report.write_text("# Findings\nDECISION: Keep it simple.\n")
    if extra_file:
        (rwt / "scratch.txt").write_text("unreviewed\n")
    state = json.loads(env["state"].read_text())
    state["tasks"].append({"id": "task_r", "task_title": "Research"})
    env["state"].write_text(json.dumps(state))
    run(env, "track", "--task", "task_r", "--run", "run_1", "--project", "acme/demo", "--acceptance", "x",
        "--kind", "report")
    run(env, "attempt", "--task", "task_r", "--dispatch", "ctx_r", "--worktree-path", str(rwt))
    jrun(env, "ingest", stdin=batch({"id": "msg_r", "type": "worker_done", "subject": "d", "body": "Done.",
                                     "payload": json.dumps({"taskId": "task_r", "dispatchId": "ctx_r",
                                                            "outcome": "succeeded", "reportPath": str(report)})}))
    jrun(env, "verify", "--task", "task_r", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_r", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_r", "--kind", "board", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "approve", "--task", "task_r", "--by", "owner", "--answer", "Approve")

    jrun(env, "accept", "--task", "task_r", "--remove-worktree")

    (rm,) = [c for c in json.loads(env["state"].read_text())["calls"] if c[:2] == ["worktree", "rm"]]
    assert ("--force" in rm) is forced


def _ticket(folder, name, title, status, blocked="", kind="task"):
    lines = [f"# {title}", "", f"Type: {kind}", f"Status: {status}"]
    if blocked:
        lines.append(f"Blocked by: {blocked}")
    lines += ["", "## What to build", "", "Do it.", "", "## Acceptance criteria", "", f"- [ ] {title} works", ""]
    (folder / name).write_text("\n".join(lines))


def test_import_tickets_maps_blockers_to_deps_and_is_idempotent(env, project):
    issues = env["tmp"] / "feat" / "issues"
    issues.mkdir(parents=True)
    _ticket(issues, "01-base.md", "Base", "ready-for-agent")
    _ticket(issues, "02-next.md", "Next", "ready-for-agent", "01-base.md")
    _ticket(issues, "03-done.md", "Done", "resolved")
    _ticket(issues, "04-after-done.md", "After done", "ready-for-agent", "03-done.md", kind="research")
    _ticket(issues, "05-triage.md", "Triage", "needs-triage")
    _ticket(issues, "06-blocked.md", "Blocked", "ready-for-agent", "05-triage.md")
    _ticket(issues, "07-needs-later.md", "Needs later", "ready-for-agent", "08-later.md")
    _ticket(issues, "08-later.md", "Later", "ready-for-agent")

    first = jrun(env, "import-tickets", "--dir", str(issues.parent), "--run", "run_1", "--project", "acme/demo")
    second = jrun(env, "import-tickets", "--dir", str(issues.parent), "--run", "run_1", "--project", "acme/demo")

    created = {Path(c["ticket"]).name: c for c in first["created"]}
    assert sorted(created) == ["01-base.md", "02-next.md", "04-after-done.md", "07-needs-later.md", "08-later.md"]
    assert created["07-needs-later.md"]["deps"] == [created["08-later.md"]["task_id"]]
    assert created["02-next.md"]["deps"] == [created["01-base.md"]["task_id"]]
    assert created["04-after-done.md"]["deps"] == [] and created["04-after-done.md"]["kind"] == "report"
    skipped = {Path(s["ticket"]).name: s for s in first["skipped"]}
    assert sorted(skipped) == ["03-done.md", "05-triage.md", "06-blocked.md"]
    assert "neither resolved nor imported" in skipped["06-blocked.md"]["reason"]
    assert len(_calls(env, "task-create")) == 5 and len(second["existing"]) == 5 and second["created"] == []


def test_imported_ticket_is_large_and_refuses_small(env, project):
    issues = env["tmp"] / "feat" / "issues"
    issues.mkdir(parents=True)
    _ticket(issues, "01-base.md", "Base", "ready-for-agent")
    (created,) = jrun(env, "import-tickets", "--dir", str(issues.parent), "--run", "run_1",
                      "--project", "acme/demo")["created"]

    proc = run(env, "start-worker", "--task", created["task_id"], "--class", "small", "--agent", "omp",
               check=False)

    assert proc.returncode == 2 and "tracked as large" in proc.stderr and _calls(env, "worker-start") == []
    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    assert db.execute("SELECT work_class FROM tasks WHERE task_id=?", (created["task_id"],)).fetchone() == ("large",)


def test_triage_here_adds_and_removes_the_folder(env, project):
    out = jrun(env, "triage-here", "--dir", str(project["repo"]))
    assert out["triage_dirs"] == [str(project["repo"].resolve())] and out["lifecycle"].endswith("orca-lifecycle.py")
    assert jrun(env, "triage-here", "--dir", str(project["repo"]), "--remove")["triage_dirs"] == []


def test_completion_for_failed_task_is_kept_acked_and_task_can_retry(env, project):
    git(project["repo"], "worktree", "remove", "--force", str(project["wt"]))
    git(project["repo"], "branch", "-D", "feat/greeting")
    _set_workers(env, _worker("ctx_1", "unverifiable"))
    jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")
    assert stage(env) == "failed"

    late = jrun(env, "ingest", stdin=batch(worker_done(outcome="failed")))
    assert late["superseded"] == 1 and late["errors"] == [] and "ack" in late
    assert stage(env) == "failed"
    git(project["repo"], "branch", "feat/retry")
    retry = jrun(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--branch", "feat/retry")
    assert retry["stage"] == "implementing"


def test_attempt_refuses_worktree_from_another_repo_even_with_the_same_branch_name(env, project):
    other = env["tmp"] / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "feat/greeting")  # same name as a branch of the project repo
    git(other, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    _set_workers(env, _worker("ctx_1", "exited", "failed"))
    jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")

    refused = run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--worktree-path", str(other),
                  check=False)
    assert refused.returncode == 2 and f"--repo path:{project['repo'].resolve()}" in refused.stderr


def test_attempt_refuses_detached_head(env, project):
    _set_workers(env, _worker("ctx_1", "exited", "failed"))
    jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")
    git(project["wt"], "checkout", "-q", "--detach")

    refused = run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--worktree-path", str(project["wt"]),
                  check=False)
    assert refused.returncode == 2 and "detached HEAD" in refused.stderr



def test_second_attempt_refused_while_first_is_active(env, project):
    refused = run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--branch", "feat/greeting",
                  check=False)
    assert refused.returncode == 2 and "ctx_1" in refused.stderr


def test_review_reuses_merge_gate_left_by_a_crash(env, project):
    state = json.loads(env["state"].read_text())
    state["tasks"].append({"id": "task_prev", "status": "blocked",
                           "spec": "Human merge gate for task_a (Add greeting). Coordinator merges only after ..."})
    state["gates"] = [{"id": "gate_prev", "task_id": "task_prev", "status": "pending", "resolution": None}]
    env["state"].write_text(json.dumps(state))

    jrun(env, "ingest", stdin=batch(worker_done()))
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    out = jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "pass", "--evidence", "ok")

    assert out["gate_id"] == "gate_prev"
    calls = json.loads(env["state"].read_text())["calls"]
    assert not any(c[:2] in (["orchestration", "task-create"], ["orchestration", "gate-create"]) for c in calls)


def _orca_db_with_worker_done(env, dispatch, body, outcome="succeeded"):
    orca_db = sqlite3.connect(env["env"]["ORCA_ORCHESTRATION_DB"])
    orca_db.execute("CREATE TABLE messages (id TEXT, type TEXT, body TEXT, payload TEXT, sequence INTEGER)")
    if body is not None:
        orca_db.execute("INSERT INTO messages VALUES ('msg_done', 'worker_done', ?, ?, 1)", (body, json.dumps(
            {"taskId": "task_a", "dispatchId": dispatch, "outcome": outcome})))
    orca_db.commit()


def test_recovery_of_missed_completion_keeps_worker_report_for_capture(env, project):
    _orca_db_with_worker_done(env, "ctx_1", "Added greet.txt. Done. Nothing left.\nDECISION: Recovered decisions count.")
    _set_workers(env, _worker("ctx_1", "unverifiable", "completed"))
    out = jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")
    assert out["decisions"][0]["verdict"] == "completed" and stage(env) == "verifying"

    replay = jrun(env, "ingest", stdin=batch({"id": "msg_done", "type": "worker_done", "subject": "done",
                                              "body": "Added greet.txt. Done. Nothing left.\nDECISION: Recovered decisions count.",
                                              "payload": json.dumps({"taskId": "task_a", "dispatchId": "ctx_1",
                                                                     "outcome": "succeeded"})}))
    assert replay["duplicates"] == 1
    jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_a", "--kind", "board", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "approve", "--task", "task_a", "--by", "owner", "--answer", "Approve")
    jrun(env, "merge", "--task", "task_a")
    assert jrun(env, "capture", "--task", "task_a")["decisions_appended"] == 1


@pytest.mark.parametrize("body, outcome", [(None, "succeeded"), ("Done.", "maybe")])
def test_recovery_without_valid_worker_report_changes_nothing(env, project, body, outcome):
    _orca_db_with_worker_done(env, "ctx_1", body, outcome)
    _set_workers(env, _worker("ctx_1", "unverifiable", "completed"))
    out = jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")

    assert out["decisions"][0]["verdict"] == "completed_without_report"
    assert stage(env) == "implementing"


def test_recovery_changes_nothing_when_orca_is_unreachable(env, project):
    git(project["repo"], "worktree", "remove", "--force", str(project["wt"]))
    state = json.loads(env["state"].read_text())
    state["fail_worker_list"] = True
    env["state"].write_text(json.dumps(state))

    out = jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")
    assert [d.get("verdict") for d in out["decisions"] if "dispatch_id" in d] == ["liveness_unknown"]
    assert stage(env) == "implementing"


def test_recovery_waits_inside_lease_and_for_live_worker(env, project):
    _set_workers(env, _worker("ctx_1", "live"))
    inside = jrun(env, "recover")
    expired_live = jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")

    assert inside["checked"] == 0
    assert expired_live["decisions"][0]["verdict"] == "live_lease_expired"
    assert stage(env) == "implementing"


def test_recovery_fails_attempt_when_worktree_and_branch_are_gone(env, project):
    git(project["repo"], "worktree", "remove", "--force", str(project["wt"]))
    git(project["repo"], "branch", "-D", "feat/greeting")
    _set_workers(env, _worker("ctx_1", "unverifiable"))
    out = jrun(env, "--now", "2100-01-01T00:00:00Z", "recover")

    assert out["decisions"][0]["verdict"] == "failed"
    assert stage(env) == "failed"


def test_status_orders_question_then_approval_then_running(env, project):
    # Second tracked task waits for approval; task_a keeps a running worker with a question.
    state = json.loads(env["state"].read_text())
    state["tasks"].append({"id": "task_b", "task_title": "Add farewell"})
    env["state"].write_text(json.dumps(state))
    run(env, "track", "--task", "task_b", "--run", "run_1", "--project", "acme/demo", "--acceptance", "x")
    run(env, "attempt", "--task", "task_b", "--dispatch", "ctx_b", "--branch", "feat/greeting")
    jrun(env, "ingest", stdin=batch({**worker_done(dispatch="ctx_b"),
                                     "payload": json.dumps({"taskId": "task_b", "dispatchId": "ctx_b",
                                                            "outcome": "succeeded"})}))
    jrun(env, "verify", "--task", "task_b", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_b", "--kind", "judge", "--verdict", "pass", "--evidence", "ok")
    jrun(env, "review", "--task", "task_b", "--kind", "board", "--verdict", "pass", "--evidence", "ok")

    orca_db = sqlite3.connect(env["env"]["ORCA_ORCHESTRATION_DB"])
    orca_db.executescript("""
        CREATE TABLE question_threads (message_id TEXT, run_id TEXT, dispatch_id TEXT, status TEXT, created_at TEXT);
        CREATE TABLE dispatch_contexts (id TEXT, task_id TEXT);
        CREATE TABLE messages (id TEXT, body TEXT, payload TEXT);
        INSERT INTO question_threads VALUES ('msg_q', 'run_1', 'ctx_1', 'pending', '2026-10-18 00:00:00');
        INSERT INTO dispatch_contexts VALUES ('ctx_1', 'task_a');
        INSERT INTO messages VALUES ('msg_q', 'Use tabs or spaces?', NULL);
    """)
    orca_db.commit()
    _set_workers(env, _worker("ctx_1", "live"))

    rows = jrun(env, "status", "--json")["rows"]

    assert [(r["kind"], r["task_id"], r["stage"]) for r in rows] == [
        ("question", "task_a", "implementing"),
        ("task", "task_b", "awaiting_approval"),
        ("task", "task_a", "implementing"),
    ]
    assert rows[0]["next"].startswith("orca-lifecycle.py answer --question msg_q")
    assert "approve --task task_b" in rows[1]["next"]


def _advance_main(project, name="later.txt", text="later\n"):
    (project["repo"] / name).write_text(text)
    git(project["repo"], "add", name)
    git(project["repo"], "commit", "-qm", f"main moves: {name}")


def test_verify_refuses_a_branch_that_is_behind_the_base(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    _advance_main(project)

    refused = run(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok", check=False)

    assert refused.returncode == 2 and "1 commit(s) behind 'main'" in refused.stderr
    assert "sync --task task_a" in refused.stderr and stage(env) == "verifying"


def test_sync_brings_the_base_into_the_branch_so_verify_pins_the_merge_target(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    _advance_main(project)

    out = jrun(env, "sync", "--task", "task_a")
    pinned = jrun(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok")["reviewed_sha"]

    assert out["merged_base"] == "main" and out["stage"] == "verifying"
    assert (project["wt"] / "later.txt").read_text() == "later\n"
    assert pinned == git(project["wt"], "rev-parse", "HEAD").strip()


def test_sync_conflict_sends_the_task_to_rework_and_leaves_the_worktree_clean(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    _advance_main(project, "greet.txt", "bonjour\n")

    out = jrun(env, "sync", "--task", "task_a")

    assert out["stage"] == "rework" and out["conflicts"] == ["greet.txt"]
    assert git(project["wt"], "status", "--porcelain") == "" and (project["wt"] / "greet.txt").read_text() == "hello\n"
    assert "merge 'main' into feat/greeting" in out["next"]


def test_verify_and_sync_refuse_uncommitted_work_in_the_worker_worktree(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    (project["wt"] / "greet.txt").write_text("hello, unsaved\n")

    verify = run(env, "verify", "--task", "task_a", "--verdict", "pass", "--evidence", "ok", check=False)
    sync = run(env, "sync", "--task", "task_a", check=False)

    for proc in (verify, sync):
        assert proc.returncode == 2 and "uncommitted" in proc.stderr and "greet.txt" in proc.stderr
    assert (project["wt"] / "greet.txt").read_text() == "hello, unsaved\n"


def test_cancel_closes_the_task_keeps_the_branch_and_tells_orca(env, project):
    out = jrun(env, "cancel", "--task", "task_a", "--by", "owner", "--reason", "owner dropped greetings")

    assert out["stage"] == "closed" and stage(env) == "closed"
    assert git(project["repo"], "branch", "--list", "feat/greeting").strip()
    calls = json.loads(env["state"].read_text())["calls"]
    update = next(c for c in calls if c[:2] == ["orchestration", "task-update"])
    assert update[update.index("--result") + 1].startswith("cancelled (by owner): owner dropped greetings")
    assert any(c[:2] == ["orchestration", "worker-stop"] for c in calls)
    assert run(env, "cancel", "--task", "task_a", "--by", "owner", "--reason", "again", check=False).returncode == 2


def test_sweep_lists_every_idle_open_task_and_precheck_exits_on_leftovers(env, project):
    jrun(env, "ingest", stdin=batch(worker_done()))
    _advance_main(project)
    later = ("--now", "2100-01-01T00:00:00Z")

    out = jrun(env, *later, "sweep")
    precheck = run(env, *later, "sweep", "--precheck", check=False)
    fresh = run(env, "sweep", "--precheck", check=False)

    (row,) = out["leftovers"]
    assert row["task_id"] == "task_a" and row["stage"] == "verifying" and row["behind_base"] == 1
    assert "sync --task task_a" in row["next"]
    assert precheck.returncode == 0 and fresh.returncode == 1  # just updated: not idle yet
    jrun(env, "cancel", "--task", "task_a", "--by", "owner", "--reason", "dropped")
    assert run(env, *later, "sweep", "--precheck", check=False).returncode == 1


def test_cancel_keeps_the_task_open_while_orca_cannot_stop_its_worker(env, project):
    state = json.loads(env["state"].read_text())
    state["fail_worker_stop"] = True
    env["state"].write_text(json.dumps(state))

    proc = run(env, "cancel", "--task", "task_a", "--by", "owner", "--reason", "dropped", check=False)

    out = json.loads(proc.stdout)
    assert proc.returncode == 1 and out["cancelled"] is False and stage(env) == "implementing"
    assert [u["dispatch_id"] for u in out["unstopped"]] == ["ctx_1"]
    later = jrun(env, "--now", "2100-01-01T00:00:00Z", "sweep", "--precheck")
    assert [r["task_id"] for r in later["leftovers"]] == ["task_a"]  # still visible, so cancel can be retried


def test_sweep_repo_filter_keeps_only_that_repos_tasks(env, project):
    other = env["tmp"] / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    later = ("--now", "2100-01-01T00:00:00Z")

    mine = jrun(env, *later, "sweep", "--precheck", "--repo", str(project["wt"]))  # a worktree of the repo counts
    theirs = run(env, *later, "sweep", "--precheck", "--repo", str(other), check=False)

    assert [r["task_id"] for r in mine["leftovers"]] == ["task_a"] and theirs.returncode == 1


def test_cancel_retries_only_the_orca_tasks_left_pending_until_none_are(env, project):
    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    db.execute("UPDATE tasks SET merge_task_id='merge_a' WHERE task_id='task_a'")
    db.commit()
    state = json.loads(env["state"].read_text())
    state["fail_task_update"] = ["task_a"]  # the merge Task update succeeds, the parent's fails
    env["state"].write_text(json.dumps(state))

    first = jrun(env, "cancel", "--task", "task_a", "--by", "owner", "--reason", "dropped")
    listed = jrun(env, "sweep", "--precheck")["leftovers"]  # no idle wait for a pending cancel

    assert first["stage"] == "closed" and first["orca_pending"] == ["task_a"]
    assert [(r["task_id"], r["orca_pending"]) for r in listed] == [("task_a", ["task_a"])]
    state = json.loads(env["state"].read_text())
    state["fail_task_update"] = []
    env["state"].write_text(json.dumps(state))
    second = jrun(env, "cancel", "--task", "task_a", "--by", "coordinator", "--reason", "retry")
    assert second["orca_pending"] == [] and run(env, "sweep", "--precheck", check=False).returncode == 1
    updates = [(c[c.index("--id") + 1], c[c.index("--result") + 1]) for c in
               json.loads(env["state"].read_text())["calls"] if c[:2] == ["orchestration", "task-update"]]
    original = "cancelled (by owner): dropped"
    assert updates == [("merge_a", original), ("task_a", original), ("task_a", original)]  # only what was pending
    assert run(env, "cancel", "--task", "task_a", "--by", "owner", "--reason", "x", check=False).returncode == 2


def test_cancel_keeps_confirmed_stops_and_the_owner_reason_when_one_worker_will_not_stop(env, project):
    run(env, "attempt", "--task", "task_a", "--dispatch", "ctx_2", "--branch", "feat/greeting", check=False)
    db = sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"])
    db.execute("INSERT INTO attempts (dispatch_id, task_id, branch, lease_expires_at, status, created_at)"
               " VALUES ('ctx_2', 'task_a', 'feat/greeting', '2100-01-01T00:00:00Z', 'active', '2026-01-01T00:00:00Z')")
    db.commit()
    state = json.loads(env["state"].read_text())
    state["fail_worker_stop"] = ["ctx_2"]
    env["state"].write_text(json.dumps(state))

    first = json.loads(run(env, "cancel", "--task", "task_a", "--by", "owner", "--reason", "dropped",
                           check=False).stdout)
    statuses = dict(sqlite3.connect(env["env"]["ORCA_LIFECYCLE_DB"]).execute(
        "SELECT dispatch_id, status FROM attempts WHERE task_id='task_a'").fetchall())
    (row,) = jrun(env, "sweep", "--precheck")["leftovers"]  # no idle wait once the owner dropped it

    assert first["stopped"] == ["ctx_1"] and statuses == {"ctx_1": "abandoned", "ctx_2": "active"}
    assert row["cancel_requested"]["reason"] == "dropped" and "do not resume" in row["next"]
    state = json.loads(env["state"].read_text())
    state["fail_worker_stop"] = []
    env["state"].write_text(json.dumps(state))
    done = jrun(env, "cancel", "--task", "task_a", "--by", "coordinator", "--reason", "retry")
    assert done["stopped"] == ["ctx_2"] and stage(env) == "closed"
    result = [c for c in json.loads(env["state"].read_text())["calls"] if c[:2] == ["orchestration", "task-update"]][-1]
    assert result[result.index("--result") + 1] == "cancelled (by owner): dropped"


def test_board_status_moves_every_orca_record_of_the_worker_folder(env, project):
    # A repo registered twice (symlink and target) lists one folder under two ids; the board may show either.
    wt = str(project["wt"])
    state = json.loads(env["state"].read_text())
    state["orca_worktrees"] = [{"id": f"old::{wt}", "path": wt}, {"id": f"new::{wt}", "path": wt},
                               {"id": "other::/elsewhere", "path": "/elsewhere"}]
    before = len(state["calls"])
    env["state"].write_text(json.dumps(state))

    jrun(env, "ingest", stdin=batch(worker_done()))

    sets = [c[c.index("--worktree") + 1] for c in json.loads(env["state"].read_text())["calls"][before:]
            if c[:2] == ["worktree", "set"]]
    assert sets == [f"id:old::{wt}", f"id:new::{wt}"]
