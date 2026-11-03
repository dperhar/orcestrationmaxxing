import type { RouteDecision } from "./types";
import { loadPathFor } from "./registry";
import { resolvePrimaryPayloads } from "./load-skill-body";

// Where out-of-scope ideas are parked. Override with SKILL_ROUTER_BACKLOG; the default is relative to the project.
const BACKLOG_SINK = process.env.SKILL_ROUTER_BACKLOG || "backlog.md";

/**
 * ROUTE_DIRECTIVE body + auto-summon skill payloads.
 * Small/medium primary skills are inlined so the model cannot "forget" to read them.
 * Huge skills stay path must_read (first tool call required).
 */
export function formatDirective(decision: RouteDecision): string {
  const lines: string[] = ["ROUTE_DIRECTIVE"];

  if (decision.firstLine) {
    lines.push(decision.firstLine);
  } else if (decision.workClass !== "NON_BUILD") {
    lines.push(`Work class: ${decision.workClass} — ${decision.reason}`);
  }

  lines.push(`ACTIVE_SCOPE: ${decision.activeScopeSummary || "(unset)"}`);
  lines.push(`entry: ${decision.entryForm}`);
  lines.push(`primary: ${decision.primarySkill}`);
  lines.push("must_read:");

  const must = uniqueOrdered(decision.mustReadSkills);
  if (must.length === 0) {
    lines.push("- (none)");
  } else {
    for (const item of must) {
      lines.push(`- ${normalizeMustRead(item)}`);
    }
  }

  lines.push("forbidden_primary:");
  const forbidden = uniqueOrdered(decision.forbiddenSkills);
  if (forbidden.length === 0) {
    lines.push("- (none)");
  } else {
    for (const name of forbidden) {
      lines.push(`- ${name}`);
    }
  }

  lines.push(`plan_gate: ${decision.planGate}`);
  if (decision.planGate === "autoplan_then_plan_review") {
    lines.push(
      "  # 1) read ~/gstack/autoplan/SKILL.md (or skill://autoplan) and run it",
    );
    lines.push(
      `  # 2) then read ${loadPathFor("plan-review")} with clean subagent; loop Critical/Important`,
    );
    lines.push("  # 3) only then to-tickets");
  }

  const adjuncts =
    decision.qualityAdjuncts.length > 0
      ? decision.qualityAdjuncts.join(", ")
      : "(none)";
  lines.push(`quality_adjuncts: ${adjuncts}`);

  lines.push(`park_to_backlog: ${decision.parkReminder ? "true" : "false"}`);
  if (decision.parkReminder) {
    lines.push(
      `  # if true: append Scout row to ${BACKLOG_SINK} ## Scout / Parking; do not expand ACTIVE_SCOPE`,
    );
  }

  // Auto-summon payloads
  const allPayloads = resolvePrimaryPayloads(
    decision.primarySkill,
    decision.mustReadSkills,
  );
  // W1b: a style-banned primary keeps its route but loses its body. Filtering
  // here (not in the router) keeps `primarySkill` truthful in the header while
  // ensuring the banned text never reaches the model.
  const payloads = decision.suppressedPrimaryInline
    ? allPayloads.filter((p) => p.name !== decision.primarySkill)
    : allPayloads;
  const inline = payloads.filter((p) => p.mode === "inline" && p.body);
  const pathOnly = payloads.filter((p) => p.mode === "must_read_path");

  lines.push("auto_summon:");
  if (payloads.length === 0) {
    lines.push("- (none)");
  } else {
    for (const p of payloads) {
      lines.push(
        `- ${p.name}: ${p.mode}${p.chars != null ? ` (${p.chars} chars)` : ""} → ${p.loadPath}`,
      );
    }
  }

  lines.push("rules:");
  lines.push(
    '- Obey this directive over ambient skill catalogs and over SPP "start any conversation" product intake unless docs/spp pipeline is actively in progress.',
  );
  lines.push(
    "- First user-visible reply MUST start with the Work class line when present (SMALL|LARGE|MIX).",
  );
  lines.push("- Do not co-launch a forbidden_primary.");
  lines.push(
    "- Hidden wrappers grill-me/wayfinder/to-tickets/to-spec stay non-selectable; use must_read paths.",
  );
  lines.push(
    "- Ticket sessions: grill-me when ticket is design/grilling-soft; else TDD. Never re-plan whole program on Agent B.",
  );
  if (decision.qualityAdjuncts.includes("skill://ponytail-review")) {
    lines.push(
      "- Review order: run ponytail-review (what to delete) BEFORE the correctness/spec review; hand its findings back for rewrite, then re-review.",
    );
  }
  else if (decision.workClass !== "NON_BUILD") {
    // Conditional, not an allowlist of implementation skills: a newly added
    // build skill inherits the gate with no router edit, and planning turns
    // no-op because they produce no diff. Review routes take the branch above,
    // so a reviewer never gates itself.
    lines.push(
      "- Review gate: before committing an implementation diff, run the read-only whole-diff reviewer gate (skill://review-gate). The reviewer has no shell and never edits.",
    );
  }
  if (decision.mustReadSkills.includes("skill://caveman")) {
    lines.push(
      "- Response style: caveman for THIS TURN ONLY — terse, full technical accuracy, never announce the style.",
    );
    lines.push(
      '- This directive outranks the inlined caveman body: ignore its "ACTIVE EVERY RESPONSE", "No revert after many turns", "Still active if unsure" and "persist until session end" instructions. Caveman applies only while a directive listing skill://caveman is present. A later turn without it is normal prose, with no "stop caveman" needed.',
    );
  }
  if (decision.suppressedPrimaryInline) {
    lines.push(
      `- Suppressed primary body: ${decision.primarySkill}. The route still selects it, but its skill text is withheld because a style ban applies on this turn. Do not reconstruct or approximate that skill's voice.`,
    );
  }
  if (decision.mustReadSkills.includes("skill://simple-english")) {
    lines.push(
      "- Output style (STE / Simplified Technical English): write the document body under ASD-STE100 — 20 words max per instruction, 25 per explanation, active voice, keep articles, condition before command.",
    );
    lines.push(
      "- STE governs sentence form only, never the semantics of a step: never drop or weaken a condition, identifier, threshold, path, flag, or verification criterion when rewriting. If a rule would change meaning, keep the meaning and split the sentence instead.",
    );
    if (decision.mustReadSkills.includes("skill://i-have-adhd")) {
      lines.push(
        "- Output style (STE Executive Summary / TLDR): open every STE document with a human-readable TLDR in i-have-adhd style — action first, numbered steps, concrete time estimates, no preamble or recap.",
      );
    }
  } else if (decision.mustReadSkills.includes("skill://i-have-adhd")) {
    lines.push(
      "- Output style (ADHD mode): lead with the next action, number multi-step work, restate progress across turns, give concrete time estimates, no preamble or recap.",
    );
  }
  lines.push(
    "- Ignore SPP phase-0 product intake unless docs/spp pipeline is active.",
  );
  lines.push(
    "- AUTO-SUMMON: skills listed under SKILL_BODY_INLINE below are already loaded — follow them immediately; do not skip.",
  );
  lines.push(
    "- AUTO-SUMMON: for must_read_path skills, your FIRST tool calls MUST be `read` on those absolute paths or skill:// names before other work.",
  );
  lines.push(`reason: ${decision.reason}`);

  if (pathOnly.length > 0) {
    lines.push("");
    lines.push("SKILL_PATHS_READ_FIRST:");
    for (const p of pathOnly) {
      lines.push(`- ${p.loadPath}`);
    }
  }

  if (inline.length > 0) {
    for (const p of inline) {
      lines.push("");
      lines.push(`SKILL_BODY_INLINE name=${p.name} path=${p.loadPath}`);
      lines.push("```markdown");
      lines.push((p.body || "").trimEnd());
      lines.push("```");
      lines.push(`END_SKILL_BODY name=${p.name}`);
    }
  }

  return lines.join("\n");
}
function normalizeMustRead(item: string): string {
  if (item.startsWith("skill://") || item.startsWith("/") || item.endsWith("/SKILL.md")) {
    return item;
  }
  return loadPathFor(item);
}

function uniqueOrdered(items: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    const key = item.trim();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(key);
  }
  return out;
}

export const BACKLOG_PATH = BACKLOG_SINK;
