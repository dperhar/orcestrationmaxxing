import { loadPathFor } from "./registry";
import { matchIntent } from "./intent-map";
import type {
  DecideRouteInput,
  OutputAudience,
  RouteDecision,
  SessionPrev,
  SessionRouteResult,
  WorkClass,
} from "./types";

const WORK_ROUTING = "skill://work-routing";
const TDD = "skill://test-driven-development";
const DEBUG = "skill://systematic-debugging";
const VERIFY = "skill://verification-before-completion";
const GRILL = "skill://grilling";
const WAYFINDER = loadPathFor("wayfinder");
const TO_TICKETS = loadPathFor("to-tickets");
const TO_SPEC = loadPathFor("to-spec");
const IMPLEMENT = loadPathFor("implement");
const AUTOPLAN = loadPathFor("autoplan");
const PLAN_REVIEW = loadPathFor("plan-review");
const ORCHESTRATION = "skill://orchestration";
const PONYTAIL = "skill://ponytail-review";
const CAVEMAN = "skill://caveman";
const ADHD = "skill://i-have-adhd";
const STE = "skill://simple-english";
const VOICE_WRITER = "skill://voice-writer";
const FINAL_EDITOR = "skill://clear-output-artifacts";

const SMALL_FORBID = [
  "wayfinder",
  "grill-me",
  "grilling",
  "to-tickets",
  "to-spec",
  "orchestration",
  "nmt-market-research",
  "ajtbd-research",
];

const TICKET_FORBID_PRIMARY = [
  "wayfinder",
  "using-super-puper-powers",
];

function base(partial: Omit<RouteDecision, "parkReminder" | "activeScopeSummary"> & {
  parkReminder?: boolean;
  activeScopeSummary?: string;
}): RouteDecision {
  return {
    parkReminder: partial.parkReminder ?? false,
    activeScopeSummary: partial.activeScopeSummary ?? "",
    ...partial,
  };
}

/**
 * Audience is decided by the caller, not by any routing rule, so it is stamped
 * once at the `decideRoute` boundary instead of on ~20 individual `base()`
 * returns. `applyStyleMatrix` reads it downstream.
 */
function withAudience(
  d: RouteDecision,
  audience: OutputAudience,
): RouteDecision {
  return { ...d, outputAudience: audience };
}

function firstLine(workClass: WorkClass, reason: string): string | null {
  if (workClass === "NON_BUILD") return null;
  return `Work class: ${workClass} — ${reason}`;
}

function summarizePrompt(prompt: string): string {
  const one = prompt.replace(/\s+/g, " ").trim();
  if (one.length <= 160) return one;
  return `${one.slice(0, 157)}...`;
}

// --- lexicon helpers ---

function has(re: RegExp, s: string): boolean {
  return re.test(s);
}

function isExplicitOverride(p: string): {
  hit: boolean;
  kind?: string;
} {
  if (has(/\bto-tickets\b/i, p)) return { hit: true, kind: "to-tickets" };
  if (has(/\bto-spec\b/i, p)) return { hit: true, kind: "to-spec" };
  if (has(/\b(?:grill\s*me|grill-me|\/grilling)\b/i, p))
    return { hit: true, kind: "grill" };
  if (has(/\bwayfinder\b/i, p)) return { hit: true, kind: "wayfinder" };
  if (has(/\bjust\s+superpowers\b/i, p))
    return { hit: true, kind: "superpowers" };
  if (has(/\bautoplan\b/i, p)) return { hit: true, kind: "autoplan" };
  if (has(/\borchestration\b/i, p)) return { hit: true, kind: "orchestration" };
  const nmt = p.match(/\bnmt-[a-z0-9-]+\b/i);
  if (nmt) return { hit: true, kind: nmt[0].toLowerCase() };
  return { hit: false };
}

function isBuildVerb(p: string): boolean {
  return has(
    /\b(?:implement|build|fix|refactor|add|create|write|rewrite|ship|deploy|migrate|wire|extend|change|update|patch|code|feature|bug|ticket|plan|spec|tdd|test-driven|work\s*class|append|edit|delete|remove|install|setup|configure)\b/i,
    p,
  );
}

function isNmtLexicon(p: string): boolean {
  return (
    has(/\bnmt-[a-z0-9-]+\b/i, p) ||
    has(
      /\b(?:market research|value proposition|go-to-market|ajtbd|jobs to be done|segment(?:ation)?|core job)\b/i,
      p,
    )
  );
}

function isOrchestrationLexicon(p: string): boolean {
  return has(
    /\b(?:orchestrat(?:e|ion)|task dag|coordinate agents|multi-agent dag|worker_done|supervised multi-agent)\b/i,
    p,
  );
}

function isBugfixLexicon(p: string): boolean {
  return has(
    /\b(?:bug|tests?\s+fail|failing test|regression|broken|throws?|exception|stack trace|not working|doesn't work|does not work)\b/i,
    p,
  );
}

function isTicketSession(p: string): boolean {
  return has(
    /\b(?:claim(?:ing)?\s+(?:the\s+)?ticket|ready-for-agent|implement\s+ticket|ticket\s+session|\.scratch\/[^\s]+\/issues\/|issues\/\d{2,}[-/]|Type:\s*grilling)\b/i,
    p,
  );
}

function isTicketSoft(p: string, input?: DecideRouteInput): boolean {
  if (input?.ticketSoft === true) return true;
  if (input?.ticketType && /grill/i.test(input.ticketType)) return true;
  return has(
    /\b(?:Type:\s*grilling|design-soft|soft\s+ticket|success criteria (?:still )?(?:unclear|soft)|grill(?:-me)?\s+(?:this\s+)?ticket)\b/i,
    p,
  );
}

function isWayfinderFog(p: string): boolean {
  return has(
    /\b(?:huge chunk|map the work|too big for one session|explorative|path(?:way)? unclear|destination unclear|proper (?:skill )?routing system|multi-session fog|unknown program|chart the (?:work|territory)|wayfind)\b/i,
    p,
  );
}

function isSoftDesign(p: string): boolean {
  return has(
    /\b(?:proper system|clean this up properly|never wonder which|what should we build|multiple shapes|success criteria unclear|soft design|design still soft|unclear success|without a fixed|explor(?:e|ative)|figure out (?:the|what)|architecture options)\b/i,
    p,
  );
}

function isHardSmall(p: string): boolean {
  // concrete one-slice: append/edit named file, single fix path
  const concreteFile = has(
    /\b(?:append|add|write|edit|patch|update|fix|insert)\b[\s\S]{0,80}\b[\w./-]+\.(?:md|ts|js|tsx|jsx|py|sh|json|yml|yaml)\b/i,
    p,
  );
  const oneSlice = has(
    /\b(?:one[- ]shot|single file|this file|just append|quick fix|one line|small change)\b/i,
    p,
  );
  const multi = isMultiSession(p);
  return (concreteFile || oneSlice) && !multi && !isSoftDesign(p) && !isWayfinderFog(p);
}

function isMultiSession(p: string): boolean {
  return has(
    /\b(?:multi[- ]session|multiple sessions|ticket graph|across sessions|several tickets|wide refactor|multi[- ]slice|program of work|many tickets|phased rollout)\b/i,
    p,
  );
}

function isMixSplit(p: string): boolean {
  return has(
    /\b(?:two[- ]agent|agent a\b|design then build|design session then|MIX\b|split design and build)\b/i,
    p,
  );
}

function isSolidPlan(p: string): boolean {
  return has(
    /\b(?:solid (?:plan|spec|design)|spec attached|plan attached|design already solid|ready for tickets|approved (?:plan|spec)|implementation plan (?:ready|approved))\b/i,
    p,
  );
}

function wantsPlanGate(p: string): boolean {
  return has(
    /\b(?:autoplan|plan[- ]review|review (?:this |the )?plan|plan ready|settled plan|ready for tickets after plan|autoplan and ticket)\b/i,
    p,
  );
}

function isClaimDone(p: string): boolean {
  return has(
    /\b(?:ship it|claim done|mark (?:as )?done|ready to (?:merge|pr|ship)|create (?:the )?pr|we're done|we are done|verification before completion)\b/i,
    p,
  );
}

function isResearchOnly(p: string): boolean {
  if (isBuildVerb(p)) return false;
  return has(
    /\b(?:what is|what's|capital of|explain|how does|summarize|tell me about|who is|when was|define)\b/i,
    p,
  );
}
function isNoPermanentChange(p: string): boolean {
  return has(
    /\b(?:experiment(?:al)?|throwaway|spike|prototype|no permanent change|no-permanent-change|do not edit|don't edit|read-only|readonly)\b/i,
    p,
  );
}

function withPlanGate(
  d: RouteDecision,
  prompt: string,
): RouteDecision {
  if (!wantsPlanGate(prompt) && d.planGate === "none") return d;
  if (!wantsPlanGate(prompt)) return d;

  const must = [...d.mustReadSkills];
  // ensure autoplan before plan-review
  const without = must.filter(
    (m) =>
      m !== AUTOPLAN &&
      m !== PLAN_REVIEW &&
      m !== "autoplan" &&
      m !== "plan-review" &&
      !m.endsWith("/autoplan/SKILL.md") &&
      !m.endsWith("/plan-review/SKILL.md"),
  );
  const ordered = [WORK_ROUTING, AUTOPLAN, PLAN_REVIEW, ...without.filter((m) => m !== WORK_ROUTING)];
  // if heading to tickets, keep to-tickets after gate
  if (!ordered.includes(TO_TICKETS) && (d.primarySkill === "to-tickets" || has(/\bticket/i, prompt))) {
    ordered.push(TO_TICKETS);
  }

  return {
    ...d,
    entryForm: "plan_review_gate",
    planGate: "autoplan_then_plan_review",
    mustReadSkills: unique(ordered),
    primarySkill:
      d.primarySkill === "to-tickets" ? "to-tickets" : d.primarySkill || "autoplan",
    reason: `${d.reason}; plan gate autoplan→plan-review before tickets`,
    firstLine:
      d.workClass === "NON_BUILD"
        ? firstLine("LARGE", "settled plan needs plan gate")
        : d.firstLine,
    workClass: d.workClass === "NON_BUILD" ? "LARGE" : d.workClass,
  };
}

function unique(items: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const i of items) {
    if (!i || seen.has(i)) continue;
    seen.add(i);
    out.push(i);
  }
  return out;
}

/**
 * Mandatory over-engineering pass before any final review.
 *
 * Keys off `primarySkill`, not prompt lexicon, so every route that lands on a
 * final-review skill inherits the policy. `formatDirective` inlines the
 * `ponytail-review` body automatically because it is a registry row under the
 * inline threshold, so no extra plumbing is needed.
 */
function withPonytailFirst(d: RouteDecision): RouteDecision {
  if (d.primarySkill !== "code-review" && d.primarySkill !== "requesting-code-review") {
    return d;
  }
  return {
    ...d,
    mustReadSkills: unique([...d.mustReadSkills, PONYTAIL]),
    qualityAdjuncts: unique([...d.qualityAdjuncts, PONYTAIL]),
    reason: `${d.reason}; ponytail-review (over-engineering pass) before final review`,
  };
}

/**
 * Product-perception work: anything shaping how a user experiences the product.
 * Compressed or controlled-language styles distort the artifact itself here, so
 * caveman, STE and voice-writer are all banned. ADHD survives because it shapes
 * the reply to the owner, not the artifact.
 */
function isProductPerception(prompt: string, d: RouteDecision): boolean {
  if (
    has(
      /\b(?:interview interpretation|customer interview|persona|icp synthesis|website writing|landing page copy|generating reviews|customer review|user perception)\b/i,
      prompt,
    )
  ) {
    return true;
  }
  const perceptionSkills = [
    "nmt-analyze-interviews",
    "nmt-craft-go-to-market",
    "nmt-craft-value-proposition",
    "frontend-design",
    "design-html",
    "design-review",
    "design-shotgun",
    "design-consultation",
    "ux-copywriting",
    "voice-writer",
  ];
  return perceptionSkills.includes(d.primarySkill);
}

/**
 * Docs class: written artifacts read as procedure or reference. STE is
 * default-on here per the owner (2026-08-03), overriding the earlier held-out trial
 * that recommended opt-in. The trial's measured risk (procedural drift) is
 * mitigated by the semantic-preservation rule in `format-directive.ts`.
 */
function isDocsOrPlanTask(prompt: string, d: RouteDecision): boolean {
  if (
    has(
      /\b(?:strategy doc|strategy file|strategy\.md|job description|plan\.md|implementation plan|handoff file|subagent dispatch|\/plan\b|readme|runbook|release notes?|simple-english|asd-ste100)\b/i,
      prompt,
    )
  ) {
    return true;
  }
  const docsSkills = [
    "simple-english",
    "plan-writing",
    "spec-writing",
    "implementation-prd",
    "document-generate",
    "document-release",
    "handoff",
    "claude-handoff",
  ];
  return docsSkills.includes(d.primarySkill);
}

/**
 * Reader-facing files and prose fields need the global two-pass editorial
 * policy. Ordinary factual chat stays outside this gate.
 */
function isHumanReadableArtifactTask(prompt: string, d: RouteDecision): boolean {
  if (isDocsOrPlanTask(prompt, d) || isProductPerception(prompt, d)) return true;
  if (
    has(
      /\b(?:write|rewrite|draft|edit|create|generate|prepare|produce|publish|append|update)\b[\s\S]{0,80}\b(?:article|artifact|brief|copy|description|document|email|error message|guide|memo|message|post|presentation|prompt|questionnaire|report|resume|slide|spreadsheet|strategy|text|yaml|json|markdown|\.md)\b/i,
      prompt,
    )
  ) {
    return true;
  }
  return (
    d.primarySkill.startsWith("nmt-") ||
    [
      "academic-writing",
      "clear-output-artifacts",
      "context-explainer",
      "context-pack",
      "deep-research",
      "voice-writer",
      "document-generate",
      "document-release",
      "docx",
      "cold-writer",
      "executive-communication",
      "implementation-prd",
      "newsroom-style",
      "pdf",
      "pptx",
      "pr-and-press",
      "simple-english",
      "spec-writing",
      "story-pitch",
      "ux-copywriting",
      "written-communication",
      "writing-prds",
      "xlsx",
    ].includes(d.primarySkill)
  );
}

function withFinalEditor(d: RouteDecision, prompt: string): RouteDecision {
  if (!isHumanReadableArtifactTask(prompt, d)) return d;
  return {
    ...d,
    mustReadSkills: unique([...d.mustReadSkills, FINAL_EDITOR]),
    qualityAdjuncts: unique([...d.qualityAdjuncts, FINAL_EDITOR]),
  };
}

/**
 * Single authority over response style. Three terminal branches, first match
 * wins, so a turn never carries two conflicting style skills.
 *
 * Authoritative, not additive: a parked-build decision is a copy of a frozen
 * `scopeDecision` finalized on an earlier turn, so every branch strips the
 * styles it does not want rather than merely abstaining.
 *
 * W1b (owner 2026-08-03): a banned primary keeps its route. Only the inlined
 * body is suppressed, via `suppressedPrimaryInline`. No silent reroute.
 */
function applyStyleMatrix(d: RouteDecision, prompt: string): RouteDecision {
  const isInternalNmt =
    d.primarySkill.startsWith("nmt-") && d.outputAudience === "internal";
  const appliesAdhd =
    !isInternalNmt &&
    !has(/\b(?:stop adhd mode|normal mode|no adhd)\b/i, prompt);

  const withAdhd = (skills: string[]): string[] =>
    appliesAdhd ? unique([...skills, ADHD]) : skills.filter((s) => s !== ADHD);

  if (isProductPerception(prompt, d)) {
    const banned = [CAVEMAN, STE, VOICE_WRITER];
    return {
      ...d,
      mustReadSkills: withAdhd(
        d.mustReadSkills.filter((s) => !banned.includes(s)),
      ),
      qualityAdjuncts: d.qualityAdjuncts.filter((s) => !banned.includes(s)),
      suppressedPrimaryInline: d.primarySkill === "voice-writer",
    };
  }

  if (isDocsOrPlanTask(prompt, d)) {
    const appliesSte = !has(/\b(?:stop ste|no ste|normal english)\b/i, prompt);
    let must = d.mustReadSkills.filter((s) => s !== CAVEMAN);
    if (appliesSte) must = unique([...must, STE]);
    else must = must.filter((s) => s !== STE);
    return {
      ...d,
      mustReadSkills: withAdhd(must),
      qualityAdjuncts: d.qualityAdjuncts.filter((s) => s !== CAVEMAN),
    };
  }

  const appliesCaveman =
    d.workClass !== "NON_BUILD" &&
    !has(/\b(?:stop caveman|normal mode|no caveman|verbose)\b/i, prompt);
  let must = d.mustReadSkills.filter((s) => s !== STE);
  must = appliesCaveman
    ? unique([...must, CAVEMAN])
    : must.filter((s) => s !== CAVEMAN);
  return {
    ...d,
    mustReadSkills: withAdhd(must),
    qualityAdjuncts: d.qualityAdjuncts.filter((s) => s !== STE),
  };
}

/**
 * Single seam every session decision passes through, so routed policy applies
 * once regardless of which return path produced the decision.
 *
 * Order matters: `applyStyleMatrix` reads `workClass` and `primarySkill`, and
 * `withPonytailFirst` changes neither, so Ponytail runs first.
 */
function finalizeDecision(d: RouteDecision, prompt: string): RouteDecision {
  return withFinalEditor(applyStyleMatrix(withPonytailFirst(d), prompt), prompt);
}

/**
 * Route the final-review intents with explicit precedence.
 *
 * `INTENT_RULES` lists `code_review` before `requesting_cr`, and neither rule
 * understands the direct `/skill:` / `skill://` forms, so plain `matchIntent`
 * mis-routes "requesting code review ..." to `code-review` and lets a direct
 * `skill://requesting-code-review` prompt fall through to the claim-done rule.
 * Precedence: direct skill form, then an explicit "request(ing) code review"
 * phrase, then generic review lexicon, then a bare "before merging".
 */
function routeFinalReview(p: string, scope: string): RouteDecision | null {
  const direct = p.match(/(?:\/skill:|skill:\/\/)(code-review|requesting-code-review)\b/i);
  let skill: "code-review" | "requesting-code-review" | null = null;
  if (direct) {
    skill = direct[1].toLowerCase() as "code-review" | "requesting-code-review";
  } else if (has(/\brequest(?:ing)? code review\b/i, p)) {
    skill = "requesting-code-review";
  } else if (has(/\b(?:code review|review (?:the )?(?:pr|branch|diff)|review since)\b/i, p)) {
    skill = "code-review";
  } else if (has(/\bbefore merging\b/i, p)) {
    skill = "requesting-code-review";
  }
  if (!skill) return null;

  const intentId = skill === "requesting-code-review" ? "requesting_cr" : "code_review";
  return base({
    workClass: "SMALL",
    entryForm: "intent_skill",
    primarySkill: skill,
    mustReadSkills: [WORK_ROUTING, `skill://${skill}`],
    forbiddenSkills: [],
    firstLine: firstLine("SMALL", `intent:${intentId} → ${skill}`),
    planGate: "none",
    qualityAdjuncts: [],
    reason: `intent map: ${intentId} → ${skill}`,
    activeScopeSummary: scope || summarizePrompt(p),
  });
}

/**
 * Raw routing rules. First matching rule wins (see plan detection order).
 * Returns a decision with no audience stamp; `decideRoute` adds it.
 */
function decideRouteRaw(input: DecideRouteInput): RouteDecision {
  const prompt = (input.prompt ?? "").trim();
  const p = prompt;
  const scope = input.activeScopeSummary ?? "";

  if (!p) {
    return base({
      workClass: "NON_BUILD",
      entryForm: "research",
      primarySkill: "",
      mustReadSkills: [],
      forbiddenSkills: [],
      firstLine: null,
      planGate: "none",
      qualityAdjuncts: [],
      reason: "empty prompt",
      activeScopeSummary: scope,
    });
  }

  // 1. Explicit override
  const ex = isExplicitOverride(p);
  if (ex.hit && ex.kind) {
    const kind = ex.kind;
    if (kind === "to-tickets") {
      return withPlanGate(
        base({
          workClass: isMixSplit(p) ? "MIX" : "LARGE",
          entryForm: "explicit_override",
          primarySkill: "to-tickets",
          mustReadSkills: [WORK_ROUTING, TO_TICKETS],
          forbiddenSkills: ["wayfinder"],
          firstLine: firstLine(
            isMixSplit(p) ? "MIX" : "LARGE",
            "explicit to-tickets override",
          ),
          planGate: "none",
          qualityAdjuncts: [],
          reason: "explicit override: to-tickets",
          activeScopeSummary: scope || summarizePrompt(p),
        }),
        p,
      );
    }
    if (kind === "to-spec") {
      return base({
        workClass: "LARGE",
        entryForm: "explicit_override",
        primarySkill: "to-spec",
        mustReadSkills: [WORK_ROUTING, TO_SPEC],
        forbiddenSkills: ["wayfinder"],
        firstLine: firstLine("LARGE", "explicit to-spec override"),
        planGate: "none",
        qualityAdjuncts: [],
        reason: "explicit override: to-spec",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    if (kind === "grill") {
      return base({
        workClass: isMixSplit(p) ? "MIX" : "LARGE",
        entryForm: "explicit_override",
        primarySkill: "grilling",
        mustReadSkills: [WORK_ROUTING, GRILL],
        forbiddenSkills: ["wayfinder"],
        firstLine: firstLine(
          isMixSplit(p) ? "MIX" : "LARGE",
          "explicit grill-me override",
        ),
        planGate: "none",
        qualityAdjuncts: [],
        reason: "explicit override: grill-me → grilling",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    if (kind === "wayfinder") {
      return base({
        workClass: isMixSplit(p) ? "MIX" : "LARGE",
        entryForm: "explicit_override",
        primarySkill: "wayfinder",
        mustReadSkills: [WORK_ROUTING, WAYFINDER],
        forbiddenSkills: ["implement charge", "immediate implement"],
        firstLine: firstLine(
          isMixSplit(p) ? "MIX" : "LARGE",
          "explicit wayfinder override",
        ),
        planGate: "none",
        qualityAdjuncts: [],
        reason: "explicit override: wayfinder",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    if (kind === "superpowers") {
      return base({
        workClass: "SMALL",
        entryForm: "explicit_override",
        primarySkill: "test-driven-development",
        mustReadSkills: [WORK_ROUTING, TDD],
        forbiddenSkills: SMALL_FORBID,
        firstLine: firstLine("SMALL", "explicit just superpowers → TDD path"),
        planGate: "none",
        qualityAdjuncts: [TDD, VERIFY],
        reason: "explicit override: just superpowers",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    if (kind === "autoplan") {
      return withPlanGate(
        base({
          workClass: "LARGE",
          entryForm: "plan_review_gate",
          primarySkill: "autoplan",
          mustReadSkills: [WORK_ROUTING, AUTOPLAN, PLAN_REVIEW],
          forbiddenSkills: ["to-tickets-before-review"],
          firstLine: firstLine("LARGE", "explicit autoplan → plan gate"),
          planGate: "autoplan_then_plan_review",
          qualityAdjuncts: [],
          reason: "explicit override: autoplan",
          activeScopeSummary: scope || summarizePrompt(p),
        }),
        p,
      );
    }
    if (kind === "orchestration") {
      return base({
        workClass: "NON_BUILD",
        entryForm: "orchestration",
        primarySkill: "orchestration",
        mustReadSkills: [ORCHESTRATION],
        forbiddenSkills: ["bare parallel"],
        firstLine: null,
        planGate: "none",
        qualityAdjuncts: [],
        reason: "explicit override: orchestration",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    if (kind.startsWith("nmt-")) {
      return base({
        workClass: "NON_BUILD",
        entryForm: "nmt",
        primarySkill: kind,
        mustReadSkills: [`skill://${kind}`],
        forbiddenSkills: ["ajtbd-research"],
        firstLine: null,
        planGate: "none",
        qualityAdjuncts: [],
        reason: `explicit override: ${kind}`,
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
  }

  // Active SPP pipeline
  if (input.hasActiveSppPipeline) {
    return base({
      workClass: "NON_BUILD",
      entryForm: "spp_pipeline",
      primarySkill: "using-super-puper-powers",
      mustReadSkills: ["skill://using-super-puper-powers"],
      forbiddenSkills: [],
      firstLine: null,
      planGate: "none",
      qualityAdjuncts: [],
      reason: "active docs/spp pipeline",
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // Final review intents, before claim-done can swallow a direct skill form
  const finalReview = routeFinalReview(p, scope);
  if (finalReview) return finalReview;

  // Claim done / ship → verification re-route
  if (isClaimDone(p)) {
    return base({
      workClass: "SMALL",
      entryForm: "hard_small",
      primarySkill: "verification-before-completion",
      mustReadSkills: [WORK_ROUTING, VERIFY],
      forbiddenSkills: ["bare success"],
      firstLine: firstLine("SMALL", "claim-done/ship → verify before completion"),
      planGate: "none",
      qualityAdjuncts: [VERIFY],
      reason: "mid-session ship/done claim",
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // 1b. Full-catalog intent overlay (auto-summon specific skills)
  const intent = matchIntent(p);
  if (intent) {
    const skillLoad = loadPathFor(intent.skill);
    const must: string[] = [];
    if (intent.workClass !== "NON_BUILD") must.push(WORK_ROUTING);
    must.push(skillLoad.startsWith("/") ? skillLoad : `skill://${intent.skill}`);
    // ensure skill:// name form when loadPath is skill://
    if (skillLoad.startsWith("skill://") && !must.includes(skillLoad)) {
      must[must.length - 1] = skillLoad;
    } else if (!skillLoad.startsWith("skill://")) {
      must[must.length - 1] = skillLoad;
    }
    for (const q of intent.qualityAdjuncts) {
      const qp = loadPathFor(q);
      must.push(qp.startsWith("/") ? qp : `skill://${q}`);
    }
    const wc = intent.workClass as WorkClass;
    const d = base({
      workClass: wc,
      entryForm: "intent_skill",
      primarySkill: intent.skill,
      mustReadSkills: unique(must),
      forbiddenSkills: intent.forbidden,
      firstLine: firstLine(wc, `intent:${intent.id} → ${intent.skill}`),
      planGate: intent.planGate,
      qualityAdjuncts: intent.qualityAdjuncts.map((q) =>
        loadPathFor(q).startsWith("/") ? loadPathFor(q) : `skill://${q}`,
      ),
      reason: `intent map: ${intent.id} → ${intent.skill}`,
      activeScopeSummary: scope || summarizePrompt(p),
    });
    return intent.planGate === "autoplan_then_plan_review" ? withPlanGate(d, p) : d;
  }

  // 2. NON_BUILD research
  if (
    isResearchOnly(p) ||
    (!isBuildVerb(p) &&
      !isNmtLexicon(p) &&
      !isOrchestrationLexicon(p) &&
      !isBugfixLexicon(p) &&
      !isSoftDesign(p) &&
      !isMultiSession(p) &&
      !isMixSplit(p))
  ) {
    if (
      !isNmtLexicon(p) &&
      !isOrchestrationLexicon(p) &&
      !isBugfixLexicon(p) &&
      !isTicketSession(p) &&
      !isWayfinderFog(p) &&
      !wantsPlanGate(p) &&
      !isSoftDesign(p) &&
      !isMultiSession(p) &&
      !isMixSplit(p)
    ) {
      return base({
        workClass: "NON_BUILD",
        entryForm: "research",
        primarySkill: "",
        mustReadSkills: [],
        forbiddenSkills: ["forced Work class"],
        firstLine: null,
        planGate: "none",
        qualityAdjuncts: [],
        reason: "non-build research/Q&A",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
  }

  // 3. NMT product strategy (no eng-implement frame)
  if (isNmtLexicon(p) && !has(/\b(?:implement|code|refactor|tdd|ticket graph)\b/i, p)) {
    const named = p.match(/\bnmt-[a-z0-9-]+\b/i)?.[0]?.toLowerCase() ?? "nmt-market-research";
    return base({
      workClass: "NON_BUILD",
      entryForm: "nmt",
      primarySkill: named,
      mustReadSkills: [`skill://${named}`],
      forbiddenSkills: ["ajtbd-research"],
      firstLine: null,
      planGate: "none",
      qualityAdjuncts: [],
      reason: "NMT product-strategy lexicon",
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // 4. orchestration
  if (isOrchestrationLexicon(p)) {
    return base({
      workClass: "NON_BUILD",
      entryForm: "orchestration",
      primarySkill: "orchestration",
      mustReadSkills: [ORCHESTRATION],
      forbiddenSkills: ["bare parallel"],
      firstLine: null,
      planGate: "none",
      qualityAdjuncts: [],
      reason: "supervised multi-agent orchestration",
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // 5. bugfix
  if (isBugfixLexicon(p)) {
    return base({
      workClass: "SMALL",
      entryForm: "bugfix",
      primarySkill: "systematic-debugging",
      mustReadSkills: [WORK_ROUTING, DEBUG, TDD, VERIFY],
      forbiddenSkills: ["investigate", "random fix"],
      firstLine: firstLine("SMALL", "bug/fix → systematic-debugging + TDD"),
      planGate: "none",
      qualityAdjuncts: [TDD, VERIFY],
      reason: "bugfix lexicon",
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // 6. Ticket session
  if (isTicketSession(p)) {
    const soft = isTicketSoft(p, input);
    if (soft) {
      return base({
        workClass: "LARGE",
        entryForm: "implement_ticket",
        primarySkill: "grilling",
        mustReadSkills: [WORK_ROUTING, GRILL, TDD, IMPLEMENT],
        forbiddenSkills: [...TICKET_FORBID_PRIMARY, "whole-program replan"],
        firstLine: firstLine(
          "LARGE",
          "ticket session soft/grilling → grill then TDD",
        ),
        planGate: "none",
        qualityAdjuncts: [TDD],
        reason: "ticket session: design/grilling-soft",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    return base({
      workClass: "LARGE",
      entryForm: "implement_ticket",
      primarySkill: "test-driven-development",
      mustReadSkills: [WORK_ROUTING, TDD, IMPLEMENT],
      forbiddenSkills: [
        ...TICKET_FORBID_PRIMARY,
        "grill-me",
        "grilling",
        "whole-program replan",
      ],
      firstLine: firstLine("LARGE", "ticket session hard → implement + TDD"),
      planGate: "none",
      qualityAdjuncts: [TDD, VERIFY],
      reason: "ticket session: hard implement",
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // 7. wayfinder fog
  if (isWayfinderFog(p)) {
    const wc: WorkClass = isMixSplit(p) ? "MIX" : "LARGE";
    const reason =
      wc === "MIX"
        ? "explorative fog + two-agent split → wayfinder (Agent A)"
        : "explorative multi-session fog → wayfinder";
    return base({
      workClass: wc,
      entryForm: "wayfinder",
      primarySkill: "wayfinder",
      mustReadSkills: [WORK_ROUTING, WAYFINDER],
      forbiddenSkills: ["immediate implement", "implement charge", "to-tickets-first"],
      firstLine: firstLine(wc, reason),
      planGate: "none",
      qualityAdjuncts: [],
      reason,
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // 8. build/change size (canon)
  // MIX explicit two-agent
  if (isMixSplit(p)) {
    const fog = isWayfinderFog(p) || isSoftDesign(p);
    if (fog || isSoftDesign(p)) {
      const softFog = isWayfinderFog(p);
      return base({
        workClass: "MIX",
        entryForm: softFog ? "wayfinder" : "soft_grill",
        primarySkill: softFog ? "wayfinder" : "grilling",
        mustReadSkills: softFog
          ? [WORK_ROUTING, WAYFINDER]
          : [WORK_ROUTING, GRILL, TO_SPEC, TO_TICKETS],
        forbiddenSkills: ["single-agent whole-program implement", "immediate implement"],
        firstLine: firstLine(
          "MIX",
          softFog
            ? "two-agent: Agent A wayfinder; Agent B ticket loop"
            : "two-agent: Agent A grill→to-spec→to-tickets; Agent B ticket+TDD",
        ),
        planGate: "none",
        qualityAdjuncts: [],
        reason: "MIX two-agent design-then-build",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    return base({
      workClass: "MIX",
      entryForm: "soft_grill",
      primarySkill: "grilling",
      mustReadSkills: [WORK_ROUTING, GRILL, TO_SPEC, TO_TICKETS],
      forbiddenSkills: ["single-agent whole-program implement"],
      firstLine: firstLine(
        "MIX",
        "two-agent design-then-build; Agent A grill path",
      ),
      planGate: "none",
      qualityAdjuncts: [],
      reason: "MIX two-agent packaging",
      activeScopeSummary: scope || summarizePrompt(p),
    });
  }

  // SMALL hard one-slice
  if (isHardSmall(p) || (isBuildVerb(p) && !isMultiSession(p) && !isSoftDesign(p) && !isWayfinderFog(p) && !isSolidPlan(p))) {
    // If multi not detected and not soft → SMALL when build-like and concrete-ish
    if (!isMultiSession(p) && !isSoftDesign(p)) {
      const d = base({
        workClass: "SMALL",
        entryForm: "hard_small",
        primarySkill: "test-driven-development",
        mustReadSkills: [WORK_ROUTING, TDD],
        forbiddenSkills: SMALL_FORBID,
        firstLine: firstLine("SMALL", "hard one-slice build → TDD"),
        planGate: "none",
        qualityAdjuncts: [TDD, VERIFY],
        reason: "SMALL hard one-slice",
        activeScopeSummary: scope || summarizePrompt(p),
      });
      return withPlanGate(d, p);
    }
  }

  // LARGE hard solid
  if (isMultiSession(p) && isSolidPlan(p) && !isSoftDesign(p)) {
    return withPlanGate(
      base({
        workClass: "LARGE",
        entryForm: "explicit_override",
        primarySkill: "to-tickets",
        mustReadSkills: [WORK_ROUTING, TO_TICKETS],
        forbiddenSkills: ["wayfinder", "mandatory wayfinder", "grill-me"],
        firstLine: firstLine("LARGE", "multi-session solid plan → to-tickets"),
        planGate: "none",
        qualityAdjuncts: [],
        reason: "LARGE hard solid design",
        activeScopeSummary: scope || summarizePrompt(p),
      }),
      p,
    );
  }

  // LARGE soft
  if (isMultiSession(p) || isSoftDesign(p)) {
    if (isWayfinderFog(p)) {
      return base({
        workClass: "LARGE",
        entryForm: "wayfinder",
        primarySkill: "wayfinder",
        mustReadSkills: [WORK_ROUTING, WAYFINDER],
        forbiddenSkills: ["immediate implement"],
        firstLine: firstLine("LARGE", "LARGE soft explorative fog → wayfinder"),
        planGate: "none",
        qualityAdjuncts: [],
        reason: "LARGE soft + fog",
        activeScopeSummary: scope || summarizePrompt(p),
      });
    }
    // short soft: grill → to-spec → to-tickets
    return withPlanGate(
      base({
        workClass: isMixSplit(p) ? "MIX" : "LARGE",
        entryForm: "soft_grill",
        primarySkill: "grilling",
        mustReadSkills: [WORK_ROUTING, GRILL, TO_SPEC, TO_TICKETS],
        forbiddenSkills: ["wayfinder-mandatory", "immediate implement"],
        firstLine: firstLine(
          isMixSplit(p) ? "MIX" : "LARGE",
          "soft multi-session → grill then to-spec→to-tickets",
        ),
        planGate: "none",
        qualityAdjuncts: [],
        reason: "LARGE/MIX soft short grill path",
        activeScopeSummary: scope || summarizePrompt(p),
      }),
      p,
    );
  }

  // Plan gate only prompts
  if (wantsPlanGate(p)) {
    return withPlanGate(
      base({
        workClass: "LARGE",
        entryForm: "plan_review_gate",
        primarySkill: "autoplan",
        mustReadSkills: [WORK_ROUTING, AUTOPLAN, PLAN_REVIEW],
        forbiddenSkills: ["to-tickets-before-review"],
        firstLine: firstLine("LARGE", "settled plan → autoplan then plan-review"),
        planGate: "autoplan_then_plan_review",
        qualityAdjuncts: [],
        reason: "plan gate overlay",
        activeScopeSummary: scope || summarizePrompt(p),
      }),
      p,
    );
  }

  // Default build → SMALL TDD if any build verb left
  if (isBuildVerb(p)) {
    return withPlanGate(
      base({
        workClass: "SMALL",
        entryForm: "hard_small",
        primarySkill: "test-driven-development",
        mustReadSkills: [WORK_ROUTING, TDD],
        forbiddenSkills: SMALL_FORBID,
        firstLine: firstLine("SMALL", "default hard build → TDD"),
        planGate: "none",
        qualityAdjuncts: [TDD, VERIFY],
        reason: "default SMALL build path",
        activeScopeSummary: scope || summarizePrompt(p),
      }),
      p,
    );
  }

  return base({
    workClass: "NON_BUILD",
    entryForm: "research",
    primarySkill: "",
    mustReadSkills: [],
    forbiddenSkills: ["forced Work class"],
    firstLine: null,
    planGate: "none",
    qualityAdjuncts: [],
    reason: "fallback non-build",
    activeScopeSummary: scope || summarizePrompt(p),
  });
}

/**
 * Public route decision — always finalized.
 *
 * One public contract: every caller gets the same decision for the same input,
 * styles included. `decideRouteForSession` layers scope/park/freeze on top and
 * calls `decideRouteRaw` directly, because it must mutate the raw decision
 * (work-class freeze) before styles are applied.
 */
export function decideRoute(input: DecideRouteInput): RouteDecision {
  const audience = input.outputAudience ?? "user";
  const raw = withAudience(decideRouteRaw(input), audience);
  return finalizeDecision(raw, input.prompt ?? "");
}

/**
 * Session wrapper: freezes activeScopeSummary + scopeDecision after first prompt;
 * re-decides on override/bug/plan/ship/wayfinder/ticket; parks unrelated builds
 * by restoring scopeDecision (not a mid-session side-quest intent).
 */
export function decideRouteForSession(
  prev: SessionPrev | null,
  prompt: string,
  opts?: { hasActiveSppPipeline?: boolean; outputAudience?: OutputAudience },
): SessionRouteResult {
  const audience: OutputAudience =
    opts?.outputAudience ?? prev?.outputAudience ?? "user";
  const p = (prompt ?? "").trim();
  const priorScope = prev?.activeScopeSummary?.trim() || "";
  const priorDecision = prev?.decision;
  const scopeDecision = prev?.scopeDecision ?? priorDecision;

  const reRoute =
    isExplicitOverride(p).hit ||
    isBugfixLexicon(p) ||
    wantsPlanGate(p) ||
    isClaimDone(p) ||
    isWayfinderFog(p) ||
    isTicketSession(p) ||
    !priorDecision;

  // Unrelated new build while scope frozen → park, restore scope-establishing decision
  if (
    priorScope &&
    priorDecision &&
    scopeDecision &&
    !reRoute &&
    isBuildVerb(p) &&
    !promptSharesScope(p, priorScope)
  ) {
    const kept: RouteDecision = {
      ...scopeDecision,
      parkReminder: true,
      activeScopeSummary: priorScope,
      reason: `${scopeDecision.reason}; unrelated build parked to backlog`,
    };
    return {
      activeScopeSummary: priorScope,
      decision: finalizeDecision(kept, p),
      scopeDecision,
    };
  }

  const scope = priorScope || summarizePrompt(p);
  // Raw, not `decideRoute`: the ticket-session branch below rewrites workClass
  // and firstLine, and styles must be applied to the final shape, not the
  // pre-freeze one. `finalizeDecision` runs once at the end of this function.
  const decision = withAudience(
    decideRouteRaw({
      prompt: p,
      hasActiveSppPipeline: opts?.hasActiveSppPipeline,
      outputAudience: audience,
      activeScopeSummary: scope,
    }),
    audience,
  );

  // Preserve work class freeze on ticket sessions when prior exists
  if (
    priorDecision &&
    decision.entryForm === "implement_ticket" &&
    priorDecision.workClass !== "NON_BUILD"
  ) {
    const frozenClass = priorDecision.workClass;
    const reason = decision.reason;
    decision.workClass = frozenClass;
    decision.firstLine =
      firstLine(frozenClass, reason.replace(/^[^—]*—\s*/, "") || reason);
    decision.activeScopeSummary = scope;
  } else {
    decision.activeScopeSummary = scope;
  }

  const finalDecision = finalizeDecision(decision, p);
  const nextScopeDecision = scopeDecision ?? finalDecision;
  return {
    activeScopeSummary: scope,
    decision: finalDecision,
    scopeDecision: nextScopeDecision,
  };
}

function promptSharesScope(prompt: string, scope: string): boolean {
  const tokens = scope
    .toLowerCase()
    .split(/[^a-z0-9./_-]+/)
    .filter((t) => t.length >= 4);
  if (tokens.length === 0) return true;
  const pl = prompt.toLowerCase();
  let hits = 0;
  for (const t of tokens.slice(0, 12)) {
    if (pl.includes(t)) hits++;
  }
  return hits >= Math.min(2, tokens.length);
}

export function isValidDecision(d: unknown): d is RouteDecision {
  if (!d || typeof d !== "object") return false;
  const o = d as RouteDecision;
  return (
    typeof o.workClass === "string" &&
    typeof o.entryForm === "string" &&
    typeof o.primarySkill === "string" &&
    Array.isArray(o.mustReadSkills) &&
    Array.isArray(o.forbiddenSkills) &&
    (o.firstLine === null || typeof o.firstLine === "string") &&
    (o.planGate === "none" || o.planGate === "autoplan_then_plan_review") &&
    Array.isArray(o.qualityAdjuncts) &&
    typeof o.parkReminder === "boolean" &&
    typeof o.reason === "string" &&
    typeof o.activeScopeSummary === "string"
  );
}
