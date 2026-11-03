/**
 * orca-triage — OMP extension for the Orca triage chat.
 *  1. Injects the control-plane snapshot (`orca-lifecycle.py reload`) on session
 *     start, after compaction, and after a session switch.
 *  2. Blocks raw `git merge` / `orca orchestration ...` commands that bypass the
 *     lifecycle registry.
 * Active only when the session cwd is inside a `triage_dirs` entry of
 * ~/Library/Application Support/orcestrationmaxxing/orca-triage.json. Fails open.
 * NOT @orca-managed-pi-extension (survives Orca updates).
 */
import { execFile, execFileSync } from "node:child_process";
import {
  blockReason, editBlockReason, isActiveDir, loadConfig, taskBlockReason, type AliasResolver, type TriageConfig,
} from "./policy";

const ROLE =
  "ROLE: you are the TRIAGE COORDINATOR for this project. You never implement code, in this repo or any other: " +
  "the extension blocks code edits and implementation subagents (`task` with code-implementer or task) here. " +
  "Every code change runs in an Orca worker worktree. Planning happens only here; workers execute and never " +
  "start in plan mode. For each request: classify it with work-routing. " +
  "SMALL: one `orca-lifecycle.py start-worker --spec ... --class small`. LARGE: cut tickets with the to-tickets " +
  "skill into .scratch/<feature>/issues/ (Status: ready-for-agent, Blocked by: ...), then `import-tickets` and " +
  "`start-worker --task <id> --class large --agent omp` for each frontier ticket. " +
  "Then `wait`, verify, review (you may run reviewer + council-strategist subagents; workers do no own review). " +
  "Owner rules: do not ask the owner routine questions. When verify, judge, and board pass and the result " +
  "matches the owner's intent, approve and merge without asking (`approve --by coordinator --answer \"owner " +
  "rule 2026-10-28: auto-merge after verify, judge, board\"`). Ask only when the result deviates from what he " +
  "wanted or a real product decision is open. Merge and deploy are separate: deploy follows the deploy runbook. " +
  "If merge is blocked by uncommitted files or a conflict, find the agent or worktree that made them (session " +
  "transcripts, mtimes), ask it to finish and signal you, wait, then merge. Never ask the owner about that and " +
  "never stash others' work. Then capture and update the live SoT docs. " +
  "No work is dropped silently: every task ends merged and captured, accepted, or cancelled. `sweep` lists the " +
  "leftovers. Finish each one. `cancel` a task, with the evidence, only when the owner dropped it (his words, " +
  "`decisions.md`, or his backlog's \"do not do\" section) or the work became unnecessary. Verify refuses a " +
  "branch that is behind the base or has uncommitted work: run `sync`. " +
  "Subagents: you may spawn only read-only ones (the extension blocks the rest): `scout` to find and read code " +
  "cheaply, `reviewer`/`security-reviewer` to review a worker's diff, `council-strategist` for judgment calls, " +
  "`improve-*` for research. If a named agent does not exist, take the closest similar read-only one; if none " +
  "fits, do that reading, review, or research yourself; code changes still go only to Orca workers. " +
  "A task call picks the agent, not the model. " +
  "Load `skill://orca-triage-loop` before the first lifecycle command. If no Run is bound, run " +
  "`orca orchestration run-create`. If the code lives in another repo, register it first (`orca repo add`, " +
  "`orca-lifecycle.py project-add`) and pass that `--project`.";

const MESSAGE_TYPE = "orca-triage/snapshot";
const RELOAD_TIMEOUT_MS = 60_000;
/** OMP gives most handlers 30 s; stop awaiting before that and let the reload finish in the background. */
const HANDLER_WAIT_MS = 25_000;
const PYTHON = "/usr/bin/python3";

type Ctx = { cwd?: string; agent?: { kind?: string } };

type Pi = {
  on: (event: string, handler: (event: never, ctx?: Ctx) => unknown) => void;
  sendMessage: (
    message: { customType: string; content: string; display: boolean },
    options: { deliverAs: "nextTurn" },
  ) => unknown;
};

function activeConfig(ctx: Ctx | undefined): TriageConfig | null {
  const cfg = loadConfig();
  if (!cfg) return null;
  return isActiveDir(ctx?.cwd ?? process.cwd(), cfg.triageDirs) ? cfg : null;
}

/** Reads `alias.<name>` from git config as seen from `cwd`. Missing alias or git error: null. */
function gitAliasResolver(cwd: string): AliasResolver {
  return (name) => {
    try {
      const out = execFileSync("git", ["config", "--get", `alias.${name}`], {
        cwd, encoding: "utf8", timeout: 2000, stdio: ["ignore", "pipe", "ignore"],
      });
      return out.trim() || null;
    } catch {
      return null;
    }
  };
}

function runReload(cfg: TriageConfig, cwd: string): Promise<string> {
  const { promise, resolve, reject } = Promise.withResolvers<string>();
  execFile(
    PYTHON,
    [cfg.lifecycle, "reload"],
    { cwd, timeout: RELOAD_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024, encoding: "utf8" },
    (err, stdout, stderr) => {
      if (!err) return resolve(stdout);
      const detail = (stderr || err.message).trim().slice(-500);
      reject(new Error(err.killed ? `timed out after ${RELOAD_TIMEOUT_MS / 1000}s` : detail));
    },
  );
  return promise;
}

async function injectSnapshot(pi: Pi, ctx: Ctx | undefined): Promise<void> {
  const cfg = activeConfig(ctx);
  if (!cfg) return;
  let content: string;
  try {
    const stdout = await runReload(cfg, ctx?.cwd ?? process.cwd());
    content = `ORCA TRIAGE CONTROL PLANE (auto-loaded by orca-triage extension)\n\n${ROLE}\n\n${stdout}`;
  } catch (e) {
    const why = e instanceof Error ? e.message : String(e);
    content =
      `ORCA TRIAGE CONTROL PLANE: auto-load FAILED (${why}).\n` +
      `Run by hand: ${PYTHON} ${cfg.lifecycle} reload`;
  }
  pi.sendMessage({ customType: MESSAGE_TYPE, content, display: false }, { deliverAs: "nextTurn" });
}

export default function orcaTriageExtension(pi: Pi): void {
  const onSnapshotEvent = async (_event: unknown, ctx?: Ctx): Promise<void> => {
    try {
      if (ctx?.agent?.kind === "sub") return;
      const done = injectSnapshot(pi, ctx).catch(() => {});
      const { promise: budget, resolve } = Promise.withResolvers<void>();
      const timer = setTimeout(resolve, HANDLER_WAIT_MS);
      await Promise.race([done, budget]);
      clearTimeout(timer);
    } catch {
      // fail open: never surface errors to the user
    }
  };

  pi.on("session_start", onSnapshotEvent);
  // Compaction drops the injected snapshot from context; switching sessions loads a different history.
  pi.on("session_compact", onSnapshotEvent);
  pi.on("session_switch", onSnapshotEvent);

  pi.on("tool_call", (event: { toolName?: string; input?: Record<string, unknown> }, ctx?: Ctx) => {
    try {
      const cfg = activeConfig(ctx);
      if (!cfg) return undefined;
      const cwd = ctx?.cwd ?? process.cwd();
      const suffix = ` (orca-lifecycle.py: ${cfg.lifecycle})`;
      if (event.toolName === "edit" || event.toolName === "write") {
        const reason = editBlockReason(event.toolName, event.input);
        return reason === null ? undefined : { block: true, reason: reason + suffix };
      }
      if (event.toolName === "task") {
        const reason = taskBlockReason(event.input);
        return reason === null ? undefined : { block: true, reason: reason + suffix };
      }
      if (event.toolName !== "bash") return undefined;
      const command = event.input?.command;
      if (typeof command !== "string") return undefined;
      const reason = blockReason(command, gitAliasResolver(cwd));
      return reason === null ? undefined : { block: true, reason: reason + suffix };
    } catch {
      return undefined; // tool_call errors fail closed in OMP, so never throw
    }
  });
}
