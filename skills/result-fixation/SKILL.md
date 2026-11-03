---
name: result-fixation
description: "Fixate a completed build or NMT deliverable: dated entry at the owning layer, session log for LARGE/MIX, AGENTS.md routing rows. Loaded by the skill-router pipeline stage `fixation`; runs after verification passes, before claiming done."
disable-model-invocation: true
---

# Result fixation

Run this protocol after verification passes and before you claim done. It applies to every completed build or change (work class SMALL, LARGE, MIX) and to every completed NMT deliverable.

## 1. Find the owning layer

The owning layer is the deepest folder whose `AGENTS.md` covers the changed paths. Tooling changes own to the tooling folder. Product work owns to the product folder. Client-shareable material owns to its deepest routed subfolder.

## 2. SMALL result

Append a dated entry of 3 to 8 lines to the owning layer's context timeline:

- Harness: the tooling folder's context log (for example `context/LOCAL_CONTEXT.md`)
- Product: the routed product `.md` timeline

State what changed, why, the tests that prove it, and the paths. Do not create a new folder. Do not create a new `.md` file.

## 3. LARGE / MIX / ticket-program result

Write `docs/session logs/<YYYY-MM-DD>-<slug>/OUTCOME.md`. Add `DECISIONS.md` in the same folder when you made design decisions. Record scope, tests, and deploy verification. Reference shape: `docs/session logs/2026-08-15-example-sot-01-03/`.

## 4. Register the routing rows

1. Add or refresh the detailed row in the owning layer's `AGENTS.md` routing table.
2. Add a one-line summary row to root `AGENTS.md` only when the change created a new folder or entrypoint, or spans layers.
3. For deeper projects, put the detailed row at the deepest layer that has an `AGENTS.md`. Give an intermediate layer a row only when routing must pass through it. The root-to-leaf read chain must reach the doc.

## 5. NMT and product results

Append the outcome to the routed product `.md` timeline. The Memory Protocol already mandates this. This step gives the pipeline's fixation stage one definition everywhere.
