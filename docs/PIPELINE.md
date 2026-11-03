# What each part does

| Part | Where | What it does in the pipeline | Ships here |
|---|---|---|---|
| `orca-lifecycle.py` | `bin/` | Registry of tasks, attempts, reviews, approvals. Commands: `start-worker`, `wait`, `sync`, `verify`, `review`, `approve`, `merge`, `accept`, `capture`, `cancel`, `sweep`, `recover`, `status`. | yes |
| orca-triage extension | `omp/orca-triage/` | In the coordinator chat: loads the control-plane snapshot at session start and after every compaction, and blocks code edits and file-changing subagents. | yes |
| skill-router extension | `omp/skill-router/` | On every turn: classifies the request (SMALL / LARGE / MIX), picks the primary skill, and injects a `ROUTE_DIRECTIVE` with must-read skills. Fails open. | yes |
| registry generator | `bin/refresh-skill-router-registry.py` | Scans the installed skills and writes `omp/skill-router/registry.ts`, then deploys the router package. Run it after you install or update skills. | yes |
| `orca-triage-loop` skill | `skills/` | The coordinator's playbook. | yes |
| `work-routing` skill | `skills/` | The SMALL / LARGE / MIX rule and which path each class takes. | yes |
| `review-gate` skill | `skills/` | Whole-diff review before a commit, by the read-only `reviewer` agent. | yes |
| `result-fixation` skill | `skills/` | Where to record the result of a finished task. | yes |
| `improve` skill | `skills/` | Quality pass with three lanes (researcher, critic, scout); the coordinator runs it after a failed review. | yes |
| Agents | `agents/` | `code-implementer`, `worker-advisor` (workers); `reviewer`, `council-strategist`, `improve-researcher`, `improve-critic`, `improve-scout` (coordinator). Models come from your OMP config (`task.agentModelOverrides`). | yes |
| Orca `orchestration` skill | upstream | The version-matched Orca orchestration guide. | no, see THIRD_PARTY.md |
| Matt Pocock skills | upstream | Grilling, wayfinder, specs, tickets. | no, see THIRD_PARTY.md |
| super-puper-powers (SPP/SDP) | upstream | Plan writing and review, TDD, debugging, verification, subagent execution. | no, see THIRD_PARTY.md |

## Order of install

1. Install Orca and OMP.
2. Install the third-party skills (commands in THIRD_PARTY.md).
3. Run `bash install.sh --apply`. It installs everything in this repository and builds the router registry from what is installed.
4. Run `bash install.sh --apply` again after you add or update any skill.

## Tests

```bash
/usr/bin/python3 -m pytest -q tests/                  # lifecycle + registry generator
cd omp/orca-triage && bun test                        # coordinator policy
# router: needs a registry, so run the generator first (install.sh does it)
cd ~/.omp/agent/extensions/skill-router && bun test
```

The router tests expect the full skill set (Matt Pocock skills and SPP/SDP installed).
