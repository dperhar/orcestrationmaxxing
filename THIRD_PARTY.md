# Third-party components

This repository does not copy third-party skill bodies. `install.sh` prints the upstream install commands, and the router registry picks the skills up from where those installers put them.

| Component | Used for | Source | License | Install |
|---|---|---|---|---|
| Orca | worktrees, terminals, orchestration Runs/Tasks/messages, automations | https://github.com/stablyai/orca | see upstream | install the Orca app |
| Orca skills `orchestration`, `orca-cli` | the coordinator loads `orchestration` before the first lifecycle command | https://github.com/stablyai/orca (`skills/`) | see upstream | `npx skills add stablyai/orca`, or `orca skills get orchestration` |
| OMP (oh-my-pi) | the agent harness, extensions, subagents | https://github.com/can1357/oh-my-pi | see upstream | install OMP |
| Matt Pocock skills: `grilling`, `grill-me`, `wayfinder`, `to-spec`, `to-tickets`, `implement`, `code-review` | soft-design grilling, specs, tickets, implementation and review routes | https://github.com/mattpocock/skills | MIT | `npx skills add mattpocock/skills` |
| super-puper-powers (SPP/SDP): `plan-writing`, `plan-review`, `test-driven-development`, `systematic-debugging`, `verification-before-completion`, `subagent-driven-development`, and the rest | planning, TDD, debugging, verification, subagent execution | https://github.com/tsergeytovarov/super-puper-powers | MIT (includes MIT-licensed work from obra/superpowers and Matt Pocock; see its `UPSTREAM.md`) | `git clone https://github.com/tsergeytovarov/super-puper-powers ~/.config/opencode/super-puper-powers`, then `omp plugin link ~/.config/opencode/super-puper-powers` |

## Optional, not shipped

The router's intent map names more skills (for example `autoplan` from gstack, `caveman`, `ponytail-review`, document and design skills). They are optional. If a named skill is not installed, the router points the agent at `skill://<name>` and continues. These skills are not copied here, because their redistribution licenses were not confirmed. Install them from their own sources if you want those routes.
