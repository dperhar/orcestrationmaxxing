/**
 * skill-router — OMP/Pi extension
 * First-turn Work class enforcement + mid-session re-route.
 * NOT @orca-managed-pi-extension (survives Orca updates).
 */
import { existsSync } from "node:fs";
import { join } from "node:path";
import {
  decideRoute,
  decideRouteForSession,
  isValidDecision,
} from "./decide-route";
import { formatDirective } from "./format-directive";
import type { OutputAudience, RouteDecision } from "./types";

const STATE_TYPE = "skill-router/state";
const DIRECTIVE_TYPE = "skill-router/directive";
const ERROR_TYPE = "skill-router/error";

type RouteState = {
  activeScopeSummary: string;
  decision: RouteDecision;
  scopeDecision?: RouteDecision;
  parkReminder?: boolean;
  updatedAt: string;
  degraded?: boolean;
};

const HOOK_POST_TIMEOUT_MS = 1000;

/**
 * Emitted verbatim when routing fails AND same-turn self-heal also fails.
 *
 * Carries the review gate: a routing failure must never open an unreviewed
 * commit path. Unlike `formatDirective` there is no decision to inspect here,
 * so the line is unconditional and its own text scopes it to build diffs.
 */
function degradedDirective(): string {
  return [
    "ROUTE_DIRECTIVE",
    "Work class: SMALL|LARGE — degraded fallback; read skill://work-routing",
    "must_read:",
    "- skill://work-routing",
    "rules:",
    "- Review gate: before committing an implementation diff, run the read-only whole-diff reviewer gate (skill://review-gate). The reviewer has no shell and never edits.",
    "reason: skill-router canary/error; operating degraded until canary passes",
  ].join("\n");
}

function decisionsEqual(a: RouteDecision, b: RouteDecision): boolean {
  return (
    a.workClass === b.workClass &&
    a.entryForm === b.entryForm &&
    a.primarySkill === b.primarySkill &&
    a.planGate === b.planGate &&
    a.parkReminder === b.parkReminder &&
    a.reason === b.reason &&
    a.firstLine === b.firstLine &&
    a.activeScopeSummary === b.activeScopeSummary &&
    a.mustReadSkills.join("\0") === b.mustReadSkills.join("\0") &&
    a.forbiddenSkills.join("\0") === b.forbiddenSkills.join("\0") &&
    a.qualityAdjuncts.join("\0") === b.qualityAdjuncts.join("\0")
  );
}

function rebuildState(sessionManager: {
  getBranch?: () => unknown[];
}): RouteState | null {
  try {
    const branch = sessionManager?.getBranch?.() ?? [];
    if (!Array.isArray(branch)) return null;
    for (let i = branch.length - 1; i >= 0; i--) {
      const entry = branch[i] as {
        type?: string;
        customType?: string;
        details?: RouteState;
        content?: string;
      };
      const customType = entry?.customType || entry?.type;
      if (customType === STATE_TYPE && entry.details) {
        return entry.details as RouteState;
      }
      // some runtimes nest under message
      const msg = entry as { message?: { customType?: string; details?: RouteState } };
      if (msg?.message?.customType === STATE_TYPE && msg.message.details) {
        return msg.message.details;
      }
    }
  } catch {
    // fail open
  }
  return null;
}

/**
 * Who reads this turn's output.
 *
 * Orca's status extension stamps `ORCA_PI_STATUS_OWNED` with the lead agent's
 * PID at load, and documents that "child agents inherit the lead's pane env"
 * (orca-agent-status.ts). A subagent therefore sees the parent's PID there
 * while running under its own — the mismatch is the subagent signal, needing
 * no new plumbing from whoever spawns it.
 *
 * Deliberately not `hasUI`: an Orca automation or `omp -p` run is headless but
 * still user-facing, and stripping reader formatting there would violate
 * "ADHD for user outputs".
 *
 * Fails toward `user`: an unstamped turn keeps reader formatting. A false
 * `internal` (the owner reads raw agent prose) costs more than a false `user`
 * (a subagent gets formatting it ignores).
 */
export function resolveOutputAudience(
  ownerPid: string | undefined,
  selfPid: number,
): OutputAudience {
  if (!ownerPid) return "user";
  return ownerPid === String(selfPid) ? "user" : "internal";
}

function hasActiveSppPipeline(cwd: string | undefined): boolean {
  if (!cwd) return false;
  try {
    return existsSync(join(cwd, "docs", "spp", "pipeline-state.md"));
  } catch {
    return false;
  }
}

function bestEffortStatusPost(
  hookEventName: string,
  extra: Record<string, unknown>,
): void {
  try {
    const port =
      process.env.ORCA_AGENT_HOOK_PORT ||
      readEndpointField("ORCA_AGENT_HOOK_PORT");
    const token =
      process.env.ORCA_AGENT_HOOK_TOKEN ||
      readEndpointField("ORCA_AGENT_HOOK_TOKEN");
    const paneKey = process.env.ORCA_PANE_KEY;
    if (!port || !token || !paneKey) return;

    const url = `http://127.0.0.1:${port}/hook/omp`;
    const body = JSON.stringify({
      paneKey,
      launchToken: process.env.ORCA_AGENT_LAUNCH_TOKEN || "",
      tabId: process.env.ORCA_TAB_ID || "",
      worktreeId: process.env.ORCA_WORKTREE_ID || "",
      payload: { hook_event_name: hookEventName, ...extra },
    });

    const controller =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = setTimeout(() => controller?.abort(), HOOK_POST_TIMEOUT_MS);
    if (typeof (timer as { unref?: () => void }).unref === "function") {
      (timer as { unref: () => void }).unref();
    }

    void fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Orca-Agent-Hook-Token": token,
      },
      body,
      ...(controller ? { signal: controller.signal } : {}),
    })
      .catch(() => {})
      .finally(() => clearTimeout(timer));
  } catch {
    // never block
  }
}

function readEndpointField(key: string): string {
  try {
    const path = process.env.ORCA_AGENT_HOOK_ENDPOINT;
    if (!path) return "";
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const fs = require("fs") as typeof import("fs");
    const contents: string = fs.readFileSync(path, "utf8");
    for (const line of contents.split(/\r?\n/)) {
      const m = line.match(/^(?:set\s+)?([A-Z0-9_]+)=(.*)$/);
      if (m && m[1] === key) return m[2].replace(/\r$/, "");
    }
  } catch {
    // ignore
  }
  return "";
}

export default function skillRouterExtension(pi: {
  on: (
    event: string,
    handler: (event: unknown, ctx?: unknown) => unknown,
  ) => void;
  appendEntry?: (type: string, details?: unknown) => void;
}): void {
  let state: RouteState | null = null;
  let degraded = false;
  let notifiedDegraded = false;

  function append(type: string, details: unknown): void {
    try {
      pi.appendEntry?.(type, details);
    } catch (err) {
      console.warn("[skill-router] appendEntry failed:", (err as Error)?.message);
    }
  }

  function runCanary(): boolean {
    try {
      const d = decideRoute({ prompt: "append x to y.md" });
      if (!isValidDecision(d) || d.workClass !== "SMALL") {
        throw new Error("canary returned invalid or non-SMALL decision");
      }
      return true;
    } catch (err) {
      const e = err as Error;
      append(ERROR_TYPE, {
        at: new Date().toISOString(),
        message: e?.message || String(err),
        stack: e?.stack,
      });
      return false;
    }
  }

  pi.on("session_start", (_event: unknown, ctx: {
    sessionManager?: { getBranch?: () => unknown[] };
    hasUI?: boolean;
    ui?: { notify?: (msg: string) => void };
  }) => {
    try {
      state = rebuildState(ctx?.sessionManager ?? {});
      const ok = runCanary();
      if (!ok) {
        degraded = true;
        if (ctx?.hasUI && !notifiedDegraded) {
          notifiedDegraded = true;
          try {
            ctx.ui?.notify?.("skill-router degraded — using work-routing fallback");
          } catch {
            // ignore
          }
        }
      } else if (degraded || state?.degraded) {
        // self-healed
        degraded = false;
        if (state) state = { ...state, degraded: false };
      }
    } catch (err) {
      degraded = true;
      append(ERROR_TYPE, {
        at: new Date().toISOString(),
        message: (err as Error)?.message || String(err),
        stack: (err as Error)?.stack,
      });
    }
  });

  pi.on(
    "before_agent_start",
    (event: { prompt?: string }, ctx: { cwd?: string; hasUI?: boolean }) => {
      try {
        const prompt = event?.prompt ?? "";
        if (!prompt.trim()) return;

        // Resolved per turn, not cached: a lead session and its subagents share
        // this process's env, so only live PID comparison separates them.
        const outputAudience = resolveOutputAudience(
          process.env.ORCA_PI_STATUS_OWNED,
          process.pid,
        );

        // degraded path: inject fallback, then try self-heal in same handler
        if (degraded) {
          let content = degradedDirective();
          try {
            const healed = decideRouteForSession(
              state
                ? {
                    activeScopeSummary: state.activeScopeSummary,
                    decision: state.decision,
                    scopeDecision: state.scopeDecision,
                  }
                : null,
              prompt,
              {
                hasActiveSppPipeline: hasActiveSppPipeline(ctx?.cwd),
                outputAudience,
              },
            );
            if (isValidDecision(healed.decision)) {
              degraded = false;
              content = formatDirective(healed.decision);
              const nextState: RouteState = {
                activeScopeSummary: healed.activeScopeSummary,
                decision: healed.decision,
                scopeDecision: healed.scopeDecision ?? healed.decision,
                parkReminder: healed.decision.parkReminder,
                updatedAt: new Date().toISOString(),
                degraded: false,
              };
              const changed =
                !state ||
                !decisionsEqual(state.decision, healed.decision) ||
                state.activeScopeSummary !== healed.activeScopeSummary;
              if (changed) append(STATE_TYPE, nextState);
              state = nextState;
              bestEffortStatusPost("skill_router_decision", {
                workClass: healed.decision.workClass,
                entryForm: healed.decision.entryForm,
                primary: healed.decision.primarySkill,
                healed: true,
              });
            } else {
              bestEffortStatusPost("skill_router_degraded", { prompt: prompt.slice(0, 200) });
            }
          } catch (healErr) {
            append(ERROR_TYPE, {
              at: new Date().toISOString(),
              message: (healErr as Error)?.message || String(healErr),
              stack: (healErr as Error)?.stack,
            });
            bestEffortStatusPost("skill_router_degraded", {
              prompt: prompt.slice(0, 200),
            });
          }

          return {
            message: {
              customType: DIRECTIVE_TYPE,
              content,
              display: false,
              details: { degraded: degraded },
            },
          };
        }

        // healthy path
        const next = decideRouteForSession(
          state
            ? {
                activeScopeSummary: state.activeScopeSummary,
                decision: state.decision,
                scopeDecision: state.scopeDecision,
              }
            : null,
          prompt,
          {
            hasActiveSppPipeline: hasActiveSppPipeline(ctx?.cwd),
            outputAudience,
          },
        );

        if (!isValidDecision(next.decision)) {
          throw new Error("decideRouteForSession returned invalid decision");
        }

        const content = formatDirective(next.decision);
        const nextState: RouteState = {
          activeScopeSummary: next.activeScopeSummary,
          decision: next.decision,
          scopeDecision: next.scopeDecision ?? next.decision,
          parkReminder: next.decision.parkReminder,
          updatedAt: new Date().toISOString(),
          degraded: false,
        };

        const changed =
          !state ||
          !decisionsEqual(state.decision, next.decision) ||
          state.activeScopeSummary !== next.activeScopeSummary;
        if (changed) append(STATE_TYPE, nextState);
        state = nextState;

        bestEffortStatusPost("skill_router_decision", {
          workClass: next.decision.workClass,
          entryForm: next.decision.entryForm,
          primary: next.decision.primarySkill,
          planGate: next.decision.planGate,
        });

        return {
          message: {
            customType: DIRECTIVE_TYPE,
            content,
            display: false,
            details: {
              workClass: next.decision.workClass,
              entryForm: next.decision.entryForm,
              primary: next.decision.primarySkill,
            },
          },
        };
      } catch (err) {
        // fail open + self-heal prep
        console.warn(
          "[skill-router] before_agent_start error:",
          (err as Error)?.message || err,
        );
        degraded = true;
        append(ERROR_TYPE, {
          at: new Date().toISOString(),
          message: (err as Error)?.message || String(err),
          stack: (err as Error)?.stack,
        });
        bestEffortStatusPost("skill_router_degraded", {
          error: (err as Error)?.message || String(err),
        });
        try {
          return {
            message: {
              customType: DIRECTIVE_TYPE,
              content: degradedDirective(),
              display: false,
              details: { degraded: true },
            },
          };
        } catch {
          return;
        }
      }
    },
  );
}

// Named exports for tests / direct import
export {
  decideRoute,
  decideRouteForSession,
  degradedDirective,
  formatDirective,
  isValidDecision,
};
