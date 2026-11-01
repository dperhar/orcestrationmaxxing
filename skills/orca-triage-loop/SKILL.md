---
name: orca-triage-loop
description: "Use in the persistent Orca triage/coordinator chat that runs project tasks of any kind (code, research, writing) as Orca workers: reload, import-tickets, start-worker, wait, answer, verify, judge, board, human approval, merge/accept, capture, recovery via orca-lifecycle.py."
---

# Orca triage loop

## TLDR

1. The `orca-triage` OMP extension loads the control-plane snapshot at session start. Act on row 1 of the attention queue.
2. Start every worker with `"$L" start-worker`. Collect every result with `"$L" wait`.
3. After `worker_done`: verify, then judge, then board. If all pass and the result matches the owner's intent, approve and merge without asking (owner rule 2026-10-28).
4. Finish with `merge` (code) or `accept` (report), then `capture`.
- The lifecycle moves each worker worktree on the Orca board by itself: working -> In progress, verify to approval -> In review, rework or abandoned -> To Fix (In progress if the board has no To Fix column), merged -> Done. Never set board statuses by hand.
- Orca links a child worktree only inside one repo, and the parent is this chat's worktree. The project's code must live in this chat's repo. If `start-worker` refuses for that reason, ask the user where the code should live. Never start a chat in the other repo or edit it yourself.
5. The extension blocks raw `git merge` and the raw `orca orchestration` verbs. Use the `"$L"` commands instead.

`L=~/Library/Scripts/orcestrationmaxxing/orca-lifecycle.py`. Load `skill://orchestration` first, then run `orca skills get orchestration`.

## Who owns which state

- Orca (`orca orchestration ...`) owns Run, Task, Dispatch, question, gate, and delivery. It is the scheduler.
- `orca-lifecycle.py` wraps the Orca calls of this loop and records project identity, kind, stage, verdicts, human approvals, deduplicated events, recovery verdicts, and context capture.
- GBrain and project Markdown files own durable knowledge. Transient task state never goes there.
- The triage chat owns nothing. A fresh session rebuilds the control surface from these stores.

## Task kinds

- `code`: the worker commits on a branch. The task ends with `merge`.
- `report`: research, writing, or analysis. The worker passes `--report-path` in worker_done. `verify` stores the report and pins its SHA-256. The task ends with `accept`, with no git merge.

## 1. One-time setup per machine and project

1. Install: `bash bin/install-orca-triage.sh --apply` (re-run after each change to the source).
2. In the triage chat's folder: `"$L" triage-here`. Restart OMP there.
3. For each repo: `orca repo add --path <main checkout>` if Orca does not list it, then `"$L" project-add --repo-path <repo> [--base-branch main] [--context-dir <dir>]`.
4. Bind the Run: `orca orchestration run-create --objective "<objective>" --json`, or `run-use --run <id>`.

## 2. Create work

- LARGE work (several slices): cut tickets with the to-tickets skill into `.scratch/<feature>/issues/NN-<slug>.md` (`Status: ready-for-agent`, `Blocked by: NN`, acceptance criteria). Then `"$L" import-tickets --dir .scratch/<feature> --run <run> --project <project>`. It maps `Blocked by:` to Orca deps and skips the rest with a reason. `Type: research` becomes a `report` task. Then `start-worker --task <id> --class large --agent omp` for each frontier ticket, all in one go.
- SMALL work (one slice): `"$L" start-worker --spec "<spec>" --title <t> --project <p> --run <r> --acceptance "<observable test>" --kind code|report --class small --agent <agent>`.
- Never implement in the triage chat. The extension blocks code edits (any path) and implementation subagents (`task` with code-implementer or the default agent). Read-only subagents stay allowed: scout, reviewer, council-strategist, improve-*.
- Code in another repo: register it first (`orca repo add --path <main checkout>`, `"$L" project-add --repo-path <repo>`) and pass that `--project`.

Every spec needs Target, Change, Constraints, Ownership, and Observable acceptance, and stays under about 300 changed lines. `start-worker` and `import-tickets` append the lifecycle contract: only this ticket (out-of-scope work goes to you as `ask`), no own review, typecheck plus related tests once, the full suite at most once, code through the `code-implementer` subagent, the `worker-advisor` subagent at decision points, worker_done with `--task-id` and `--dispatch-id`, then stop.

## 3. Start and wait

1. Start: `"$L" start-worker --task <t> --class small|large --agent <agent>`. `--class` is required; it is a size label (imported tickets are large and refuse `small`). Every OMP worker starts with the lifecycle overlay: no plan mode, no runtime advisor. The start submits the brief if Orca leaves it in the input box. It chooses `--repo`, `--worktree new-child`, `--name`, and `--base-branch`. It reopens a `completed` Task, adds `--retry-of` for a `failed`/`blocked` Task, and records the Dispatch.
2. Wait: `"$L" wait`. It records each batch, acks it only after it is recorded, and lists the questions. A worker_done without `--task-id` settles through its recorded Dispatch.
- If `wait` keeps reporting the same unprocessable message, run `"$L" discard --message <id> --reason "<why>"`, then `wait` again. The reason stays on record and the batch is acked.
- At verify, run the full test suite once on the branch and once on the base branch. Compare the failure names: only new failures belong to the worker.
3. For each question, ask the user if it is a user decision. Then run `"$L" answer --question <id> --body "<answer>"`.
4. If `wait` exits 1, fix each error (for example, `track` and `attempt` for an unknown Dispatch), then run `wait` again.

## 4. After a successful worker_done

1. Release the worker terminal as the Orca guide requires (`orca orchestration worker-release --dispatch <id>`).
2. Run the acceptance test. Record it: `"$L" verify --task <t> --verdict pass|fail --evidence "<command + result>"`.
3. Judge: for code, write `git diff <base>...<branch>` and `git log <base>..<branch> --name-status` to a file. For a report, use the report path. Give that file to the `reviewer` role (it has no shell). Record: `"$L" review --task <t> --kind judge --verdict pass|fail --evidence "<summary + file>"`.
4. Board: spawn `council-strategist` for scope and fit. Record it with `--kind board`.
5. If any verdict is `fail`, run the `improve` skill on the evidence. Then run `"$L" start-worker --task <t> --class <same class> --agent <agent>`.
6. If all verdicts pass, the task is `awaiting_approval`. Owner rule (2026-10-28): do not ask the owner routine questions. If the result matches what he asked for, approve it yourself: `"$L" approve --task <t> --by coordinator --answer "owner rule 2026-10-28: auto-merge after verify, judge, board"`. Ask him with the ask tool only when the result deviates from his intent or a real product decision is open. Then record his verbatim answer.
7. To send work back, run `"$L" reject --task <t> --by <name> --answer "<verbatim answer>" --reason "<why>"`.
8. Finish: `"$L" merge --task <t> --into <branch>` (code) or `"$L" accept --task <t>` (report). Merge and deploy are separate: merge after the reviews, deploy only by the project's deploy runbook. `merge` keeps uncommitted files that the branch does not change and lists them as `kept_dirty`. It refuses when an uncommitted or untracked file is one the branch changes, or when the merge conflicts. Then find the agent or worktree that made those files (session transcripts, file mtimes), ask it to finish and signal you, wait for the signal, and merge again. Never ask the owner about this and never stash others' work. If the branch itself is wrong, run `reject`, then `start-worker`. Every worktree of the task (earlier attempts too) goes to Done with its terminals closed. Add `--remove-worktree` to delete them instead.
9. Run `"$L" capture --task <t> [--gbrain]`. `DECISION:` lines go to the project's `decisions.md`: a short operational log, one line each (created if missing).
10. If `capture` lists `sot_pending`, the workers changed reality. You, the triage agent, now rewrite the live SoT documents in the project folder to match it: architecture (`ARCHITECTURE.md`, `Architecture/`), business (`business.md`, `Business/`), dev (`Dev/`), product (`Product/`). Edit in place; these are live documents, not logs. Then run `"$L" sot-done --task <t> --file <doc> [--file <doc> ...]`. It refuses until every pending area is covered by a document changed after capture. It commits the tracked documents and closes the task.

## 5. Recovery

`"$L" recover` checks attempts with an expired lease (15 minutes after the last heartbeat). `reload` runs it.

- `abandoned`: Orca proved that the worker exited without worker_done. The branch stays. Inspect `git log <branch>`, then `start-worker`.
- `failed`: the worktree and the branch are gone.
- `live_lease_expired`: Orca sees a live agent. Read it with `orca orchestration worker-read --dispatch <id>`.
- `stale_unverified`: no heartbeat for 30 minutes and no exit proof. Inspect with `worker-show`. Do not retry yet.
- `liveness_unknown`: `worker-list` failed. Nothing changed. Run `recover` again later.
- `completed` / `completed_without_report`: Orca shows the work as done. With a valid stored report, recovery settles the task. Without one, nothing changes.
- Never delete a branch to recover. Never call `worker-abandon` or `worker-stop` without the proof that the Orca guide requires.

## Rules

- Never mark a Task completed by hand. worker_done settles Tasks.
- Merge gates sit on a separate `merge:` Task. A gate on a completed worker Task reopens it.
- `verify` pins the commit or the report. If the branch moves after that, `review` sends the task back to `verifying`, and `merge` refuses the new commits.
- `decisions.md` is the only file `capture` creates. SoT documents are updated by you, never appended to as logs.
- `"$L" status` is the attention queue: P1 question or gate, P2 approval, P3 rework or recovery, P4 verify or review, P5 running.
