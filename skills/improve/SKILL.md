---
name: improve
description: |
  Quality pass over the current conversation, run by the owner with `/improve`. Re-reads every user
  input, recovers the stated intent, enriches from the sources with material that could change
  the answer, attacks the result through a researcher lane, a critic lane, and a fact lane, and ends in an
  actionable todo list. Never invoked by the model: triggered only by the `/improve` command
  (or the phrases улучши / перепроверь / ты уверен?).
disable-model-invocation: true
---

# improve

Three jobs, in order:

1. **Intent recovery** (always) — re-read every user input in this conversation and prove each intent landed.
2. **Enrichment** (always) — go back to the sources for material that could *change* the answer, not confirm it, and for the links between sources nobody has drawn.
3. **Verification** (ask first) — re-check what was already found. Optional; not the default.

Every pass ends in a **todo list**, plus the questions the pass could not answer.

Then **reconciliation**: write what the pass learned back into the source `.md` files.

Hard cap: 3 passes. Gains plateau after 1–3; past that, self-bias makes it worse.

## Subagent routing (OMP)

Two lanes plus facts. You, the parent, own the intent ledger, the merge, every edit, and the todo list. No subagent rewrites the artifact.

- **`improve-researcher`** — Pass 2 enrichment: new material that would change a conclusion, plus cross-source links. Returns tagged claims.
- **`improve-critic`** — Pass 3: YES/NO checks per ledger row and grounded attacks on weak claims. Returns cited findings. Runs on a different model family than the parent so it does not share your blind spots.
- **`improve-scout`** — facts only: where a claim lives, quotes, counts. Runs on the cheap pinned subscription model (`agentModelOverrides["improve-scout"]`); no verdicts.

Run researcher first, feed its output to the critic. Skip a lane when its pass is trivially empty (no sources to mine, artifact under ~20 lines). All three can be pinned to models in `task.agentModelOverrides`. Never send thinking work to `task` or `sonic`.

## Pass 0 — recover the user's intent

Do this before touching the artifact.

**Scroll back through the whole conversation and re-read every user turn, first to last.** Not your summary of it — the actual messages. Include the one-liners, the interjections, the corrections, the polite asides. Compaction and your own narration are lossy, and interjections are exactly where the user corrects you.

- A file, screenshot, or link in a user turn is part of that turn. Open it. An attachment is usually the entire point of the message.
- If earlier turns are genuinely unavailable because context was compacted, say so out loud and ask the user to re-state what you cannot see. Do not silently proceed on a partial history.
- Then check the worktree for intent the user wrote down instead of typing at you: plan files, `.scratch/`, `docs/`, the routing table in `AGENTS.md` / `CLAUDE.md`, and any `.md` touched recently for this task. Read what is relevant; those files are ground truth and outrank your memory.

Build an **intent ledger** — one row per distinct thing the user asked for:

| # | verbatim quote | what it means | status | evidence |

- `status` is `honored` / `partial` / `missed` / `withdrawn`.
- `withdrawn` only when a later turn overrides it — quote that turn too.
- `evidence` is a file path and line, a command's output, or a quote from the artifact. "It's handled" is not evidence.
- A requirement stated once counts exactly as much as one repeated three times.

Read *intent*, not keywords. "If you figure out a clever way to X, that'd be great" is a requirement, phrased politely. A throwaway aside naming a constraint is a constraint.

If the artifact does not exist yet, the ledger is the spec.

**Sources can be wider than this worktree — but only just.** A workspace may hold dozens of unrelated worktrees; reading all of them is noise, not diligence. Widen only to worktrees **related to this task**, and establish that from evidence, not a guess: the routing table in `AGENTS.md` / `CLAUDE.md`, long-term memory if the harness has it, the branch or worktree the user names, and creation order — the immediate predecessor is usually the one they mean by "the previous worktree". Confirm relatedness by the work inside, not by a similar name. Two related worktrees is the normal case; more than three means you are guessing — ask instead. In each one you do open, include the throwaway output: subagent reports, `.scratch/` temp files, and any `.md` a prior agent generated. That material is where the previous pass buried what it could not use.

## Pass 1 — ask what this pass is for

Build the question from the ledger, not from a template. The options are the concrete gaps you just found, named in the user's own words.

One ask-tool call (`ask` in OMP, `AskUserQuestion` in Claude/Cursor), up to three questions:

1. **Focus** — `multi: true`. Up to 4 options, each a real `partial` or `missed` ledger row: label is its shorthand, description is what is wrong with it right now. Recommend the one whose failure invalidates the most downstream work.
2. **Re-verify what's already there?** — `Spot-check load-bearing claims only` (recommended) / `Skip, trust it` / `Full re-verify against every source`.
3. **How far to reach for new material** — `Sources in the worktree` / `+ targeted web research` (recommend when any claim is about the outside world) / `+ parallel subagents over the source corpus` (recommend when the corpus is more than ~10 files).

Drop any question already answered. No `partial` or `missed` rows? Drop question 1 and say so. Already dispatched the subagents or research? Drop question 3 — never ask permission for a decision you have taken. Ask nothing and skip the call entirely if the user's request already names the focus, depth, and reach. No answer? Take the recommended options and record them as a one-line `Assumptions` note in your reply.

## Pass 2 — enrich (mandatory)

Enrichment is not re-reading to confirm. It is hunting for what would **change** the answer.

First snapshot the artifact: every name, number, date, claim, section header. That is your rollback target.

Then, following the direction the user's thinking was already going:

1. **Extend, don't re-tread.** For each focused ledger row: what would the sources say about this that nobody has quoted yet? Go get it.
2. **Follow the user's grain.** If they have been pulling on segments, pull harder on segments. Do not pivot to pricing because pricing is easier to research.
3. **Index, then link.** Reading file by file cannot produce cross-file findings — the link only appears once the claims sit in one list. So first extract every claim, entity, number, and decision from *all* sources into one flat index, tagged with its source. Then walk it for pairs: same entity in two files, one file's conclusion contradicting another's premise, a number restated differently, a decision in one place whose consequence is unstated in another. `A→B` in one file and `B→C` in a second, with `A→C` written nowhere, is the classic non-obvious finding (Swanson linking). Report each as: the two sources, the link, and what it changes.
4. **Reach outward** only as far as Pass 1 chose. Current year in queries. Named practitioners and papers over listicles. Three independent sources is signal; one blog post is one opinion. Tag every new claim `established` / `emerging` / `single-source` / `speculative`.
5. **Common-sense gate.** New material earns a place in the artifact only if it changes a conclusion, a number, a priority, or removes an unknown. Interesting-but-inert findings go in a one-line "also found" list.

Anything that contradicts the artifact is a finding, not a nuisance. Surface it with both sources and their dates.

## Pass 3 — critique as someone else

Never critique in your own voice. You recognize your own writing and prefer it, and the bias compounds each pass. Two structural counters, both cheap:

- **Ground every finding.** A critique with no artifact behind it is a guess. Self-critique from memory alone is measured to be neutral or harmful; self-critique against re-read sources, tool output, or fetched material is what works. Every `weak` / `wrong` label cites a path, a command's output, or a quote. No citation, no finding.
- **Ask in binary.** "Is this section good?" invites a vague yes. Turn each ledger row into one or more questions answerable only YES or NO — "Does §3 name the file it edits?", "Is every number in the table traceable to a source?". Decomposed binary checks agree with human judgment far more than holistic scoring, and they leave a trail you can re-run next pass.

Then take an adversarial reviewer role — you did not write this, and your job is to attack it. Per element:

- Does it satisfy the ledger row it claims to satisfy?
- Is it as specific as the sources now allow, after enrichment?
- What would falsify it?
- What does the user's toughest reader attack first?

Label each `strong` / `weak` / `missing` / `wrong`. `strong` means lower priority this pass, not exempt.

**Keep disagreement alive.** When two readings of the same element conflict, do not average them into a safe verdict — a conflict you resolve too early is a finding you deleted. Report both and say which you acted on.

Filters running the whole time:

- **MECE** — every structural element complete, none overlapping.
- **Concrete over abstract** — every "several", "various", "significant" becomes the actual thing.
- **Contradiction sweep** — re-use the Pass 2 index: any key claim that disagrees with another source gets both quoted, with dates and paths. Newer wins by default; say out loud that you applied that default.
- **Fresh-reader test** — could an agent with zero conversation history act on this without asking a question?
- **Drift check** — re-read the ledger. Did the improvement wander off what was asked?

## Pass 4 — edit surgically, then regress

Edit only `weak` / `missing` / `wrong` sections. Full rewrites carry the most regression risk; leave `strong` sections alone unless the user asks.

Risk order, safest first: polish, add missing content, correct facts, restructure. Restructuring is the most dangerous — only on request, and lock every number and name before moving anything.

**Pre-mortem before you touch anything.** Assume this edit round already failed. In three lines: how did it fail, which section did it break, what would have caught it? Then edit to avoid that. Doing this first cuts revision churn, which matters under a 3-pass cap.

Then check against the snapshot:

- every name, number, date, specific claim still present and still right?
- every section header still there?
- any new claim with no source behind it?
- any concrete detail replaced by a vague summary?
- anything added that nobody asked for?

Any failure: revert that section, retry narrower.

Between passes, collapse the critique to one line — what changed, what is still open. Do not carry the full critique history forward; it pollutes the next pass.

**Stop** when every ledger row is `honored` or explicitly `withdrawn`, enrichment turned up nothing that changes a conclusion, and any scores the user gave are 4+/5. Then name the ceiling: "near the ceiling without [a user decision / this specific data / an expert opinion]".

## Output — findings become a todo list

A finding stated in prose is a finding nobody actions. Close every pass with a checklist, ordered so the item whose failure invalidates the most downstream work sits first:

`[ ]` — one bounded action, its ledger row, its target file or section, and how you will know it is done.

Use the harness todo tool when there is one, so state survives the turn. Split the list in two: **do now** (fixes this pass) and **later** (out of scope, parked). Anything that needs a decision you cannot make goes in a third group — **needs the user** — with the specific question attached, not a vague flag.

**Non-obvious holes become grilling questions.** A gap you found but cannot close from the sources is not a defect in the pass — it is the highest-value output of it. Collect those separately as questions to put to the user, sharpest first. The unstated assumption, the number nobody sourced, the decision made in one file and contradicted in another: each is one question. This list is the handoff to a grilling or design session.

## Reconciliation — write findings back to the sources

Stale `.md` files poison every future session. This runs once the user accepts the output, and immediately whenever they say "that's wrong in the doc", "this is outdated", or "we changed this".

1. **Read the routing table first** — the entry point (`AGENTS.md` / `CLAUDE.md`). It decides which file an edit goes in. No routing table: ask which file. Never guess, and never create a new `.md`. If nothing fits, propose a routing rule, not a new file.
2. **Classify each discrepancy** — `stale` (was true, isn't now) / `missing` (decided but never written down) / `contradictory` (two files disagree) / `vague` (this pass forced it concrete).
3. **Propose, grouped by file.** Per edit: section anchor phrase, current text, proposed text with today's date, one-sentence trigger, edit type. Strategy decisions update in place with `(updated YYYY-MM-DD)`. Data and events append, following that file's own stated ordering convention.
4. **Every proposal gets an explicit approved / rejected / modified.** If the user answers the output but ignores the proposals, resurface them — up to 3 times, then note the open ones once and move on. "Skip all" or "later" ends it.
5. **Apply approved edits** — re-read the target from disk first, edit, then confirm in one line per file what changed.
6. **Then check**: any dead cross-reference created? anything now duplicated? file count unchanged — it must be. Lean beats complete; an edit that adds maintenance cost without proportional value should not have been proposed.
