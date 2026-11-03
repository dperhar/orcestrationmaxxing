import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import {
  decideRoute,
  decideRouteForSession,
  isValidDecision,
} from "./decide-route";
import { formatDirective } from "./format-directive";
import { degradedDirective, resolveOutputAudience } from "./index";
import {
  HIDDEN_COMMANDS,
  loadPathFor,
  primaryForGroup,
  registryByName,
  SKILL_REGISTRY,
} from "./registry";

describe("registry", () => {
  test("every collision group has one primary or is adjunct-only/dead", () => {
    const groups = new Map<string, typeof SKILL_REGISTRY>();
    for (const row of SKILL_REGISTRY) {
      const list = groups.get(row.collisionGroup) ?? [];
      list.push(row);
      groups.set(row.collisionGroup, list);
    }
    for (const [group, rows] of groups) {
      const primaries = rows.filter((r) => r.role === "primary");
      const onlyAdjunctOrDead = rows.every(
        (r) => r.role === "adjunct" || r.role === "dead" || r.role === "hidden-command",
      );
      if (primaries.length === 0) {
        // hidden-command groups may pair with a primary body (grill→grilling)
        if (group === "grill") {
          expect(registryByName("grilling")?.role).toBe("primary");
          continue;
        }
        if (onlyAdjunctOrDead) continue;
      }
      expect(primaries.length).toBe(1);
    }
  });

  test("hidden commands keep disableModelInvocation and load paths", () => {
    for (const name of HIDDEN_COMMANDS) {
      const row = registryByName(name);
      expect(row).toBeDefined();
      expect(row!.disableModelInvocation).toBe(true);
      expect(row!.role).toBe("hidden-command");
      const path = loadPathFor(name);
      expect(path.length).toBeGreaterThan(0);
    }
    // grill-me loads grilling body
    expect(loadPathFor("grill-me")).toBe("skill://grilling");
    expect(loadPathFor("wayfinder")).toContain("/wayfinder/SKILL.md");
    expect(loadPathFor("to-tickets")).toContain("/to-tickets/SKILL.md");
    expect(loadPathFor("to-spec")).toContain("/to-spec/SKILL.md");
  });

  test("tdd collision winner is SDP test-driven-development", () => {
    const p = primaryForGroup("tdd");
    expect(p?.name).toBe("test-driven-development");
    expect(registryByName("tdd")?.role).toBe("dead");
  });

  test("canary shape import works", () => {
    const d = decideRoute({ prompt: "append x to y.md" });
    expect(isValidDecision(d)).toBe(true);
  });
});

describe("decideRoute scenarios", () => {
  test("append X to file Y → SMALL TDD; forbid wayfinder/grill/to-tickets", () => {
    const d = decideRoute({
      prompt: "append the LaunchAgents list to LOCAL_CONTEXT.md",
    });
    expect(d.workClass).toBe("SMALL");
    expect(d.entryForm).toBe("hard_small");
    expect(d.mustReadSkills.join(" ")).toContain("work-routing");
    expect(d.mustReadSkills.join(" ")).toContain("test-driven-development");
    expect(d.forbiddenSkills).toEqual(
      expect.arrayContaining(["wayfinder", "grill-me", "to-tickets"]),
    );
    expect(d.planGate).toBe("none");
    expect(d.firstLine).toMatch(/^Work class: SMALL —/);
  });

  test("multi-session explorative proper skill routing → LARGE/MIX wayfinder", () => {
    const d = decideRoute({
      prompt:
        "we need a proper skill routing system across multi-session work; path unclear, map the work, too big for one session",
    });
    expect(["LARGE", "MIX"]).toContain(d.workClass);
    expect(d.entryForm).toBe("wayfinder");
    expect(d.primarySkill).toBe("wayfinder");
    expect(d.mustReadSkills.some((m) => m.includes("wayfinder"))).toBe(true);
    expect(d.mustReadSkills.join(" ")).toContain("work-routing");
    expect(d.forbiddenSkills.join(" ")).toMatch(/implement/i);
    expect(d.planGate).toBe("none");
  });

  test("multi-session solid plan → LARGE to-tickets; no mandatory wayfinder", () => {
    const d = decideRoute({
      prompt:
        "multi-session program; solid plan attached and design already solid — break into tickets",
    });
    expect(d.workClass).toBe("LARGE");
    expect(d.primarySkill).toBe("to-tickets");
    expect(d.mustReadSkills.some((m) => m.includes("to-tickets"))).toBe(true);
    expect(d.forbiddenSkills.join(" ")).toMatch(/wayfinder/i);
    expect(d.planGate).toBe("none");
  });

  test("soft multi-session one grill unlocks tickets → grilling chain", () => {
    const d = decideRoute({
      prompt:
        "multi-session feature; success criteria unclear but not a huge map — soft design, one grill should unlock tickets",
    });
    expect(["LARGE", "MIX"]).toContain(d.workClass);
    expect(d.entryForm).toBe("soft_grill");
    expect(d.primarySkill).toBe("grilling");
    expect(d.mustReadSkills).toEqual(
      expect.arrayContaining([
        expect.stringContaining("grilling"),
        expect.stringContaining("to-spec"),
        expect.stringContaining("to-tickets"),
      ]),
    );
    expect(d.planGate).toBe("none");
  });

  test("explicit two-agent design then build → MIX", () => {
    const d = decideRoute({
      prompt:
        "explicit two-agent split: design then build; Agent A sharpens, Agent B implements tickets",
    });
    expect(d.workClass).toBe("MIX");
    expect(d.entryForm).toBe("soft_grill");
    expect(d.primarySkill).toBe("grilling");
    expect(d.firstLine).toMatch(/^Work class: MIX —/);
    expect(d.forbiddenSkills.join(" ")).toMatch(/whole-program|single-agent/i);
  });

  test("claim ready-for-agent ticket Type grilling → grill then tdd", () => {
    const d = decideRoute({
      prompt:
        "claim ready-for-agent ticket .scratch/skills-cleanup/issues/04-agents.md Type: grilling — success criteria still soft",
    });
    expect(d.entryForm).toBe("implement_ticket");
    expect(d.primarySkill).toBe("grilling");
    expect(d.mustReadSkills.join(" ")).toContain("grilling");
    expect(d.mustReadSkills.join(" ")).toContain("test-driven-development");
    expect(d.forbiddenSkills.join(" ")).toMatch(/wayfinder/i);
    expect(d.forbiddenSkills.join(" ")).toMatch(/replan/i);
  });

  test("claim ready-for-agent hard implement ticket → tdd", () => {
    const d = decideRoute({
      prompt:
        "claim ready-for-agent ticket issues/02-decide-route.md and implement the pure decideRoute module",
    });
    expect(d.entryForm).toBe("implement_ticket");
    expect(d.primarySkill).toBe("test-driven-development");
    expect(d.mustReadSkills.join(" ")).toContain("test-driven-development");
    expect(d.forbiddenSkills).toEqual(
      expect.arrayContaining(["grill-me", "grilling", "wayfinder"]),
    );
  });

  test("to-tickets for this → LARGE/MIX to-tickets path", () => {
    const d = decideRoute({ prompt: "to-tickets for this approved plan" });
    expect(["LARGE", "MIX"]).toContain(d.workClass);
    expect(d.primarySkill).toBe("to-tickets");
    expect(d.mustReadSkills.some((m) => m.includes("to-tickets"))).toBe(true);
  });

  test("wayfinder this migration → wayfinder path", () => {
    const d = decideRoute({
      prompt: "wayfinder this migration across the monorepo",
    });
    expect(["LARGE", "MIX"]).toContain(d.workClass);
    expect(d.primarySkill).toBe("wayfinder");
    expect(d.mustReadSkills.some((m) => m.includes("wayfinder"))).toBe(true);
  });

  test("nmt market research → nmt, not ajtbd", () => {
    const d = decideRoute({ prompt: "nmt-market-research for Acme segments" });
    expect(d.entryForm).toBe("nmt");
    expect(d.workClass).toBe("NON_BUILD");
    expect(d.firstLine).toBeNull();
    expect(d.primarySkill).toContain("nmt");
    expect(d.forbiddenSkills).toContain("ajtbd-research");
  });

  test("task DAG coordinate agents → orchestration", () => {
    const d = decideRoute({
      prompt: "orchestrate a task DAG and coordinate agents with worker_done",
    });
    expect(d.entryForm).toBe("orchestration");
    expect(d.primarySkill).toBe("orchestration");
    expect(d.forbiddenSkills).toContain("bare parallel");
  });

  test("capital of France → NON_BUILD no Work class", () => {
    const d = decideRoute({ prompt: "What is the capital of France?" });
    expect(d.workClass).toBe("NON_BUILD");
    expect(d.firstLine).toBeNull();
    // ADHD is global (R8): a NON_BUILD turn carries no routing skills, but the
    // reader-facing style still applies.
    expect(d.mustReadSkills).toEqual(["skill://i-have-adhd"]);
    expect(d.forbiddenSkills).toContain("forced Work class");
  });

  test("tests fail in foo → bugfix systematic-debugging + tdd", () => {
    const d = decideRoute({ prompt: "tests fail in foo after the last refactor" });
    expect(d.entryForm).toBe("bugfix");
    expect(d.primarySkill).toBe("systematic-debugging");
    expect(d.mustReadSkills.join(" ")).toContain("systematic-debugging");
    expect(d.mustReadSkills.join(" ")).toContain("test-driven-development");
    expect(d.forbiddenSkills.join(" ")).toMatch(/investigate|random/i);
  });

  test("plan ready review it → plan gate autoplan then plan-review", () => {
    const d = decideRoute({
      prompt: "plan ready, review it then autoplan and ticket this",
    });
    expect(d.planGate).toBe("autoplan_then_plan_review");
    const joined = d.mustReadSkills.join("\n");
    const autoIdx = joined.indexOf("autoplan");
    const reviewIdx = joined.indexOf("plan-review");
    expect(autoIdx).toBeGreaterThanOrEqual(0);
    expect(reviewIdx).toBeGreaterThan(autoIdx);
    expect(d.forbiddenSkills.join(" ")).toMatch(/to-tickets-before-review|before-review/i);
  });

  test("mid-session ship it → verification-before-completion", () => {
    const d = decideRoute({ prompt: "ship it, we're done" });
    expect(d.primarySkill).toBe("verification-before-completion");
    expect(d.mustReadSkills.join(" ")).toContain("verification-before-completion");
    expect(d.forbiddenSkills).toContain("bare success");
  });
});


describe("intent auto-summon", () => {
  test("voice-writer for channel post routes, but W1b withholds its body", () => {
    const d = decideRoute({ prompt: "написать пост для канала про harness" });
    expect(d.primarySkill).toBe("voice-writer");
    expect(d.entryForm).toBe("intent_skill");
    expect(d.suppressedPrimaryInline).toBe(true);
    const text = formatDirective(d);
    // Route still names it; only the skill text is withheld.
    expect(text).toContain("primary: voice-writer");
    expect(text).not.toContain("SKILL_BODY_INLINE name=voice-writer");
  });

  test("decideRoute and decideRouteForSession agree on styles", () => {
    // One public contract: the session wrapper adds scope/park/freeze, never a
    // different style outcome for the same prompt.
    for (const prompt of [
      "написать пост для канала про harness",
      "write a strategy doc for the harness",
      "append x to y.md",
      "what is the capital of France",
    ]) {
      const direct = decideRoute({ prompt });
      const session = decideRouteForSession(null, prompt).decision;
      expect(session.mustReadSkills).toEqual(direct.mustReadSkills);
      expect(session.suppressedPrimaryInline ?? false).toBe(
        direct.suppressedPrimaryInline ?? false,
      );
    }
  });

  test("pdf skill for .pdf path", () => {
    const d = decideRoute({ prompt: "extract text from report.pdf" });
    expect(d.primarySkill).toBe("pdf");
    expect(formatDirective(d)).toContain("auto_summon:");
  });

  test("impeccable for distinctive landing UI", () => {
    const d = decideRoute({ prompt: "build a distinctive landing page UI" });
    expect(d.primarySkill).toBe("impeccable");
    expect(d.entryForm).toBe("intent_skill");
    expect(d.forbiddenSkills).toContain("frontend-design");
  });

  test("boardroom decision", () => {
    const d = decideRoute({ prompt: "boardroom GO/NO-GO on this launch" });
    expect(d.primarySkill).toBe("boardroom");
  });
});

describe("final review routing", () => {
  test("code_review build intent routes to code-review with ponytail first", () => {
    const d = decideRouteForSession(
      null,
      "code review this branch before we ship the implementation",
    ).decision;
    expect(d.primarySkill).toBe("code-review");
    expect(d.mustReadSkills).toContain("skill://ponytail-review");
    expect(d.qualityAdjuncts).toContain("skill://ponytail-review");
    expect(d.reason).toContain(
      "ponytail-review (over-engineering pass) before final review",
    );
    expect(formatDirective(d)).toContain(
      "- Review order: run ponytail-review (what to delete) BEFORE the correctness/spec review; hand its findings back for rewrite, then re-review.",
    );
  });

  test("requesting_cr build intent routes to requesting-code-review with ponytail first", () => {
    const d = decideRouteForSession(
      null,
      "requesting code review before merging this feature build",
    ).decision;
    expect(d.primarySkill).toBe("requesting-code-review");
    expect(d.mustReadSkills).toContain("skill://ponytail-review");
    expect(d.qualityAdjuncts).toContain("skill://ponytail-review");
  });

  test("direct skill forms for final review keep their own primary skill and get ponytail", () => {
    const cases: Array<[string, string]> = [
      ["/skill:code-review review the implementation before merging", "code-review"],
      [
        "skill://requesting-code-review final review this shipped diff",
        "requesting-code-review",
      ],
    ];
    for (const [prompt, expected] of cases) {
      const d = decideRouteForSession(null, prompt).decision;
      expect(d.primarySkill).toBe(expected);
      expect(d.mustReadSkills).toContain("skill://ponytail-review");
      expect(d.qualityAdjuncts).toContain("skill://ponytail-review");
    }
  });

  test("spec_review remains NON_BUILD and gets no ponytail requirement", () => {
    const d = decideRouteForSession(null, "spec-review the approved spec").decision;
    expect(d.workClass).toBe("NON_BUILD");
    expect(d.primarySkill).toBe("spec-review");
    expect(d.mustReadSkills).not.toContain("skill://ponytail-review");
  });

  test("ship phrase with requesting review routes to requesting-code-review with ponytail", () => {
    const d = decideRouteForSession(
      null,
      "/skill:requesting-code-review final code review before ready to merge",
    ).decision;
    expect(d.primarySkill).toBe("requesting-code-review");
    expect(d.mustReadSkills).toContain("skill://ponytail-review");
    expect(d.qualityAdjuncts).toContain("skill://ponytail-review");
  });

  test("non-review work carries no ponytail requirement", () => {
    const d = decideRouteForSession(null, "spec-review the approved spec").decision;
    expect(d.workClass).toBe("NON_BUILD");
    expect(d.mustReadSkills).not.toContain("skill://ponytail-review");
    expect(d.qualityAdjuncts).not.toContain("skill://ponytail-review");
    expect(formatDirective(d)).not.toContain("- Review order:");
  });
});

describe("caveman response style", () => {
  test("build work gets caveman and the override rule", () => {
    const d = decideRouteForSession(
      null,
      "add a row-count floor to the refresher",
    ).decision;
    expect(d.workClass).toBe("SMALL");
    expect(d.mustReadSkills).toContain("skill://caveman");
    expect(formatDirective(d)).toContain("outranks the inlined caveman body");
  });

  test("NON_BUILD work never gets caveman", () => {
    const d = decideRouteForSession(null, "spec-review the approved spec").decision;
    expect(d.workClass).toBe("NON_BUILD");
    expect(d.mustReadSkills).not.toContain("skill://caveman");
    expect(formatDirective(d)).not.toContain("outranks the inlined caveman body");
  });

  test("explicit opt-out suppresses caveman on build work", () => {
    const d = decideRouteForSession(
      null,
      "add a row-count floor to the refresher, normal mode",
    ).decision;
    expect(d.mustReadSkills).not.toContain("skill://caveman");
  });

  test("caveman is revoked on a turn that opts out", () => {
    const t1 = decideRouteForSession(null, "add a row-count floor to the refresher");
    expect(t1.decision.mustReadSkills).toContain("skill://caveman");

    // The frozen scopeDecision already carries caveman, so a later opt-out turn
    // proves withCaveman strips an inherited entry rather than merely abstaining.
    const t2 = decideRouteForSession(
      {
        activeScopeSummary: t1.activeScopeSummary,
        decision: t1.decision,
        scopeDecision: t1.scopeDecision,
      },
      "add a row-count floor to the refresher, normal mode",
    );
    expect(t2.decision.mustReadSkills).not.toContain("skill://caveman");

    const t3 = decideRouteForSession(
      {
        activeScopeSummary: t2.activeScopeSummary,
        decision: t2.decision,
        scopeDecision: t2.scopeDecision,
      },
      "add a row-count floor to the refresher",
    );
    expect(t3.decision.mustReadSkills).toContain("skill://caveman");
  });
});

describe("decideRouteForSession", () => {
  test("freezes activeScopeSummary after first prompt", () => {
    const first = decideRouteForSession(null, "append x to y.md");
    expect(first.activeScopeSummary).toContain("append x to y.md");
    const second = decideRouteForSession(
      first,
      "also rewrite the entire billing system multi-session",
    );
    expect(second.activeScopeSummary).toBe(first.activeScopeSummary);
    expect(second.decision.parkReminder).toBe(true);
  });

  test("re-routes on bug lexicon mid-session", () => {
    const first = decideRouteForSession(null, "append x to y.md");
    const second = decideRouteForSession(first, "tests fail in the append path");
    expect(second.decision.entryForm).toBe("bugfix");
    expect(second.decision.primarySkill).toBe("systematic-debugging");
  });

  test("ticket session freezes prior work class when LARGE/MIX", () => {
    const a = decideRouteForSession(
      null,
      "multi-session soft design success criteria unclear for routing",
    );
    expect(["LARGE", "MIX"]).toContain(a.decision.workClass);
    const b = decideRouteForSession(
      a,
      "claim ready-for-agent ticket issues/02-x.md and implement",
    );
    expect(b.decision.entryForm).toBe("implement_ticket");
    expect(b.decision.workClass).toBe(a.decision.workClass);
  });

  test("park restores scope-establishing decision after side-quest intent", () => {
    const a = decideRouteForSession(
      null,
      "append the LaunchAgents list to LOCAL_CONTEXT.md",
    );
    expect(a.decision.workClass).toBe("SMALL");
    expect(a.decision.primarySkill).toBe("test-driven-development");
    const b = decideRouteForSession(a, "написать пост для канала про harness");
    expect(b.decision.primarySkill).toBe("voice-writer");
    expect(b.activeScopeSummary).toBe(a.activeScopeSummary);
    const c = decideRouteForSession(
      b,
      "also rewrite the entire billing system multi-session",
    );
    expect(c.decision.parkReminder).toBe(true);
    expect(c.activeScopeSummary).toBe(a.activeScopeSummary);
    // park must restore original build primary, not voice-writer side quest
    expect(c.decision.primarySkill).toBe("test-driven-development");
    expect(c.decision.workClass).toBe("SMALL");
  });
});

describe("pre-commit review gate", () => {
  const GATE =
    "- Review gate: before committing an implementation diff, run the read-only whole-diff reviewer gate (skill://review-gate). The reviewer has no shell and never edits.";

  test("build routes carry the gate regardless of which skill implements", () => {
    // Deliberately not an allowlist of skill names: a newly added
    // implementation skill must inherit the gate without a router edit.
    for (const prompt of [
      "append x to y.md",
      "fix the bug in decide-route.ts",
      "ship it, ready to merge",
      "autoplan and ticket this settled plan",
      "write a spec for this feature",
    ]) {
      const d = decideRouteForSession(null, prompt).decision;
      expect(d.workClass).not.toBe("NON_BUILD");
      expect(formatDirective(d)).toContain(GATE);
    }
  });

  test("review routes are exempt so a reviewer never gates itself", () => {
    for (const prompt of [
      "code review this branch before we ship the implementation",
      "requesting code review before merging this feature build",
    ]) {
      const d = decideRouteForSession(null, prompt).decision;
      expect(formatDirective(d)).not.toContain("- Review gate:");
    }
  });

  test("NON_BUILD work carries no review gate", () => {
    for (const prompt of [
      "spec-review the approved spec",
      "What is the capital of France?",
    ]) {
      const d = decideRouteForSession(null, prompt).decision;
      expect(d.workClass).toBe("NON_BUILD");
      expect(formatDirective(d)).not.toContain("- Review gate:");
    }
  });
});

describe("degraded fallback", () => {
  test("carries the review gate: a routing failure must not open a commit path", () => {
    // The gate lives in formatDirective, but index.ts emits degradedDirective()
    // verbatim when routing AND same-turn self-heal both fail. Without the gate
    // there, every build handled while degraded can reach commit unreviewed.
    expect(degradedDirective()).toContain("- Review gate:");
  });
});

describe("formatDirective", () => {
  test("emits ROUTE_DIRECTIVE with Work class line for SMALL", () => {
    const d = decideRoute({ prompt: "append x to y.md" });
    const text = formatDirective(d);
    expect(text.startsWith("ROUTE_DIRECTIVE")).toBe(true);
    expect(text).toContain("Work class: SMALL");
    expect(text).toContain("must_read:");
    expect(text).toContain("skill://work-routing");
    expect(text).toContain("forbidden_primary:");
    expect(text).toContain("plan_gate: none");
    expect(text).toContain("park_to_backlog: false");
  });

  test("omits Work class line for NON_BUILD", () => {
    const d = decideRoute({ prompt: "What is the capital of France?" });
    const text = formatDirective(d);
    expect(text).not.toMatch(/^Work class:/m);
    expect(text).toContain("entry: research");
  });

  test("plan gate includes ordered comments", () => {
    const d = decideRoute({ prompt: "autoplan and ticket this settled plan" });
    const text = formatDirective(d);
    expect(text).toContain("plan_gate: autoplan_then_plan_review");
    expect(text).toContain("autoplan");
    expect(text).toContain("plan-review");
  });
});


describe("output formatting matrix", () => {
  test("ADHD applies to every turn, dev and non-dev", () => {
    const build = decideRouteForSession(null, "append x to y.md").decision;
    expect(build.mustReadSkills).toContain("skill://i-have-adhd");

    const nonBuild = decideRouteForSession(null, "what is the capital of France").decision;
    expect(nonBuild.workClass).toBe("NON_BUILD");
    expect(nonBuild.mustReadSkills).toContain("skill://i-have-adhd");
  });

  test("explicit opt-out drops ADHD", () => {
    const d = decideRouteForSession(null, "append x to y.md, stop adhd mode").decision;
    expect(d.mustReadSkills).not.toContain("skill://i-have-adhd");
  });

  test("docs class gets STE plus ADHD TLDR, never caveman", () => {
    for (const prompt of [
      "write a strategy doc for the harness",
      "write a job description for a senior engineer",
      "write the handoff file for the subagent dispatch",
    ]) {
      const d = decideRouteForSession(null, prompt).decision;
      expect(d.mustReadSkills).toContain("skill://simple-english");
      expect(d.mustReadSkills).toContain("skill://i-have-adhd");
      expect(d.mustReadSkills).not.toContain("skill://caveman");
      const text = formatDirective(d);
      expect(text).toContain("Output style (STE / Simplified Technical English)");
      expect(text).toContain("Output style (STE Executive Summary / TLDR)");
    }
  });

  test("STE directive forbids semantic drift in procedures", () => {
    const d = decideRouteForSession(null, "write a strategy doc for the harness").decision;
    expect(formatDirective(d)).toContain(
      "STE governs sentence form only, never the semantics of a step",
    );
  });

  test("build turns get caveman, never STE", () => {
    const d = decideRouteForSession(null, "append x to y.md").decision;
    expect(d.mustReadSkills).toContain("skill://caveman");
    expect(d.mustReadSkills).not.toContain("skill://simple-english");
  });

  test("product perception bans caveman, STE and voice-writer body but keeps ADHD", () => {
    const d = decideRouteForSession(
      null,
      "customer interview interpretation and persona ICP synthesis",
    ).decision;
    expect(d.mustReadSkills).toContain("skill://i-have-adhd");
    expect(d.mustReadSkills).not.toContain("skill://caveman");
    expect(d.mustReadSkills).not.toContain("skill://simple-english");
    expect(d.mustReadSkills).not.toContain("skill://voice-writer");
    expect(d.qualityAdjuncts).not.toContain("skill://caveman");
  });

  test("every routed human-readable artifact gets the final editorial pass", () => {
    for (const prompt of [
      "write a strategy doc for the harness",
      "написать пост для канала про harness",
      "nmt-market-research for Acme",
      "write release notes for version 0.2.0",
      "rewrite this error message",
    ]) {
      const d = decideRouteForSession(null, prompt).decision;
      expect(d.mustReadSkills).toContain("skill://clear-output-artifacts");
      expect(d.qualityAdjuncts).toContain("skill://clear-output-artifacts");
    }
  });

  test("artifact finalizer survives product-perception style exclusions", () => {
    const d = decideRouteForSession(
      null,
      "customer interview interpretation and persona ICP synthesis",
    ).decision;
    expect(d.mustReadSkills).toContain("skill://clear-output-artifacts");
    expect(d.mustReadSkills).not.toContain("skill://simple-english");
  });

  test("plain factual answers do not load the artifact finalizer", () => {
    const d = decideRouteForSession(null, "what is the capital of France").decision;
    expect(d.mustReadSkills).not.toContain("skill://clear-output-artifacts");
  });

  test("W1b: voice-writer primary keeps the route but suppresses its inlined body", () => {
    const d = decideRouteForSession(null, "написать пост для канала про harness").decision;
    expect(d.primarySkill).toBe("voice-writer");
    expect(d.suppressedPrimaryInline).toBe(true);
    const text = formatDirective(d);
    expect(text).not.toContain("SKILL_BODY_INLINE name=voice-writer");
    expect(text).toContain("- Suppressed primary body: voice-writer");
  });

  test("NMT keeps ADHD for user outputs and drops it for internal", () => {
    const user = decideRouteForSession(null, "nmt-market-research for Acme", {
      outputAudience: "user",
    }).decision;
    expect(user.primarySkill).toBe("nmt-market-research");
    expect(user.mustReadSkills).toContain("skill://i-have-adhd");

    const internal = decideRouteForSession(null, "nmt-market-research for Acme", {
      outputAudience: "internal",
    }).decision;
    expect(internal.primarySkill).toBe("nmt-market-research");
    expect(internal.mustReadSkills).not.toContain("skill://i-have-adhd");
  });
});
describe("W3: output audience detection", () => {
  // Orca's status extension stamps ORCA_PI_STATUS_OWNED with the lead agent's
  // PID at load (orca-agent-status.ts: "child agents inherit the lead's pane
  // env"). A child runs under a different PID, so a mismatch identifies a
  // subagent without needing any new plumbing from the spawner.
  test("lead agent is user-facing: owner PID matches this process", () => {
    expect(resolveOutputAudience(String(process.pid), process.pid)).toBe("user");
  });

  test("subagent is internal: inherited owner PID belongs to the parent", () => {
    expect(resolveOutputAudience("424242", process.pid)).toBe("internal");
  });

  test("no owner stamp defaults to user, never internal", () => {
    // Plain `omp -p`, CI, or a turn before the status extension loaded. A
    // user-facing turn must never lose reader formatting by accident.
    expect(resolveOutputAudience(undefined, process.pid)).toBe("user");
    expect(resolveOutputAudience("", process.pid)).toBe("user");
  });

  test("headless is not internal: audience never consults hasUI", () => {
    // An Orca automation run has no UI, but the owner still reads its output.
    // Only PID identity decides, so headless user runs keep ADHD.
    expect(resolveOutputAudience(String(process.pid), process.pid)).toBe("user");
  });
});
