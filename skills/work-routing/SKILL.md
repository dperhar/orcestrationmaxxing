---
name: work-routing
description: "Auto-classify every multi-step build/change as SMALL/LARGE/MIX on first reply (announce Work class without the owner asking). Soft design auto-starts grill-me; hard one-slice work implements same session. Use for any implementation, harness change, refactor, product build, or when superpowers/to-tickets/grill routing is relevant — the owner will not say classify or grill me."
---

# Work routing (build tasks)

Durable MECE rule for OMP/Orca agents: choose **do-it-now superpowers/SDP execution** vs **to-tickets**, including the **MIX** two-agent path.

Always-on summary: root `AGENTS.md` → `## Work routing (build tasks)`.

## Critical: silent defaults (the owner will not prompt this)

The owner types **normal task language only**. He will **not** say “classify”, “announce SMALL/LARGE/MIX”, or “grill me”.

You MUST still:

1. On the **first user-visible reply** of a build/change task, start with exactly:
   `Work class: SMALL|LARGE|MIX — <one-line why>`
2. Then follow **only** that path (no mixed modes).
3. If design is **soft**, invoke `grill-me` **before** any full plan, tickets, or multi-step implement — without being asked.
4. If design is **hard** and one-slice, treat as **SMALL**: research/plan as needed, then implement. Do **not** interview him for sport.

### Soft vs hard (auto, no magic words)

**Soft → grill first** (LARGE with grill, or MIX) when any of:

- success criteria unclear (“never wonder which…”, “proper system”, “clean this up properly”)
- multiple plausible product/architecture shapes
- multi-session program without a fixed first deliverable
- he is choosing *what* to build, not only *how* to apply a fixed edit

**Hard → SMALL** when all of:

- concrete deliverable (file/path/behavior named or obvious)
- one vertical slice fits one context window
- early discoveries won’t invalidate a multi-ticket graph
- example: “append a live LaunchAgents list to LOCAL_CONTEXT.md”

## Naming map

| Owner language | Invoke |
|---|---|
| superpowers / writing plans | `plan-writing` (SDP) then `subagent-driven-development` + `test-driven-development` |
| grill-me | `grill-me` → `grilling` (**auto** when design soft) |
| wayfinder | `wayfinder` (**auto** on explorative multi-session fog under LARGE/MIX Agent A) |
| to-spec | `to-spec` |
| to-tickets | `to-tickets` |
| implement one ticket | If ticket Type/body is grilling or design-soft → `grill-me` first, then `implement` + TDD; else `implement` + `test-driven-development`; `plan-writing` only for that ticket if multi-step |

Do **not** install live `obra/superpowers`. SDP is live. Priority: **NMT → gstack/gbrain → SDP → SP**.

## MECE decision rule (canonical)

```
On any build/change request (code, harness script, product feature, multi-step fix):

1) CLASSIFY once on the first user-visible line:
   Work class: SMALL|LARGE|MIX — <one-line why>
   Then follow only that path. The owner will not ask you to classify.

A. SMALL — superpowers path (same session)
   Criteria (all true):
   - One vertical slice fits one fresh context window
   - Interfaces/seams known enough that early discoveries will not invalidate later plan steps
   - No multi-ticket dependency graph needed
   - Deliverable is concrete (hard design)
   Action:
   - Optional short plan / OMP plan mode
   - Execute via test-driven-development + subagent-driven-development (or direct implement if ≤1 task)
   - Do NOT open to-tickets
   - Do NOT grill / interview for requirements already fixed

B. LARGE — tickets path
   Criteria (any true):
   - Multiple vertical slices / multi-session work
   - Early implementation can invalidate later detailed plan steps
   - Wide refactor needing expand–contract batches
   Action:
   - If design soft + explorative fog (path/destination not visible, huge unknown map) → **wayfinder** first (may grill inside)
   - If design soft but short (one grill unlocks tickets) → auto grill-me first, then to-spec, then to-tickets
     (the owner will not say “grill me”)
   - If design already solid in conversation/spec → to-tickets only
   - One ticket per session/context; clear context between tickets
   - Per ticket: if ticket is design/grilling-soft → grill-me first; else implement + TDD (or plan-writing for that ticket only)

C. MIX — two-agent / two-session pipeline
   Criteria: LARGE + needs design grilling / product-language sharpening before tickets
   Action:
   - Session/agent 1 (design): same as LARGE soft (wayfinder if fog else grill-me → to-spec → to-tickets)
   - Session/agent 2+ (build): one ready-for-agent ticket → grill if soft else plan if needed → TDD → review → commit
   - Never re-plan the whole program inside a ticket agent
   - Keep the MIX label (do not collapse into LARGE-only)

2) Non-build tasks (pure research Q&A, pure writing, triage-finding, one-shot harness ops
   with no multi-step change) may skip this router — but if the task becomes a multi-step
   change mid-flight, classify then.

3) User override wins only when the owner explicitly names a path
   ("to-tickets", "just superpowers", "grill me"). Silence is not an override.
```

## First-reply checklist

1. Emit `Work class: …` (mandatory).
2. Soft? → start `grill-me` immediately (MIX/LARGE soft). No full plan dump first.
3. SMALL hard? → gather facts if needed → plan only if multi-step → implement. No tickets. No grilling.
4. LARGE hard? → `to-tickets` (skip grill).
5. Do not wait for the owner to say “announce class” or “grill me”.

## Ticket execution loop

1. Matt config: `docs/agents/issue-tracker.md` (else run `setup-matt-pocock-skills`, local `.scratch/`).
2. Claim frontier `ready-for-agent` / unblocked ticket.
3. Plan **that ticket only** if multi-step.
4. `implement` + `test-driven-development` (+ `subagent-driven-development` if needed).
5. Code-review → commit → mark resolved → next session for next ticket.
6. Fixate (`skill://result-fixation`): SMALL → dated timeline append at the owning layer; LARGE/MIX/ticket programs → `docs/session logs/<date>-<slug>/OUTCOME.md` (+ `DECISIONS.md`); routing rows: detail at owning-layer AGENTS.md, root summary only for new entrypoints/cross-layer.

## Precedence

- User explicit path + this router override SDP “start any conversation → product pipeline” for eng build routing.
- SPP product phases only for product-pipeline work (`docs/spp/…`).
- Orca setup script only verifies policy (`orca-worktree-setup.sh`); it is not a second author.

## Anti-patterns

- Silent classification (acting without `Work class:` line).
- Waiting for the owner to request grilling when design is soft.
- Grilling a hard SMALL task (“append X to Y”).
- Opening `to-tickets` for a one-context append/fix.
- Re-planning the whole program inside a single ticket agent.

## Automatic enforcement

- OMP extension `~/.omp/agent/extensions/skill-router/` injects `ROUTE_DIRECTIVE` on every `before_agent_start` (first-turn + mid-session re-route on bug/plan/ship/ticket/wayfinder/override).
- Fail-open: canary/error → degraded fallback (`skill://work-routing` only) then self-heal on next successful decide — no user reinstall path.
- Plan gate on settled plans: `autoplan` → `plan-review` → `to-tickets` (wrapper owns gate; never patch gstack `autoplan/SKILL.md`).
- Hidden skills stay `disable-model-invocation: true`; directive supplies absolute/`skill://` load paths (`grill-me` → `skill://grilling`).
- Out-of-scope discoveries → Scout row in the project's `backlog.md` `## Scout / Parking` (not eng `.scratch` tickets).
- Eng tracker: `.scratch/<feature>/` via `docs/agents/issue-tracker.md`. Codex mirror: `~/.codex/AGENTS.md`.
- Full-catalog intent map (`intent-map.ts`, 100+ rules) auto-selects primary skills (voice-writer, nmt-*, pdf/docx/pptx, frontend-design, boardroom, …).
- Auto-summon: skill bodies ≤16KB are inlined into ROUTE_DIRECTIVE; larger skills listed under SKILL_PATHS_READ_FIRST (first tool calls must `read` them).
- Directive carries an ordered `pipeline:` block on build routes: execute stages in order; review-gate and verification cannot be skipped; any plan written in plan mode must embed these stages, fixation last.
- Done includes fixation (`skill://result-fixation`, providerRank 10): SMALL → dated timeline append at the owning layer; LARGE/MIX → `docs/session logs/<date>-<slug>/OUTCOME.md`; routing rows: detail at owning-layer AGENTS.md, root summary only for new entrypoints/cross-layer.
