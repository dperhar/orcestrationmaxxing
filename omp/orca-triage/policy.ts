/**
 * orca-triage policy — pure logic, no OMP imports.
 *
 * Decides (a) whether a session cwd is an Orca triage chat and (b) whether a
 * bash command bypasses the lifecycle registry (`orca-lifecycle.py`).
 * The scanner is a best-effort shell tokenizer, not a shell: it covers chained
 * commands, quotes, env/sudo wrappers, `sh -c`, `eval`, `$(...)` and backticks.
 */
import { readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";

export type TriageConfig = { triageDirs: string[]; lifecycle: string };

export const CONFIG_PATH = join(
  homedir(),
  "Library",
  "Application Support",
  "orcestrationmaxxing",
  "orca-triage.json",
);

export const DEFAULT_LIFECYCLE = join(
  homedir(),
  "Library",
  "Scripts",
  "orcestrationmaxxing",
  "orca-lifecycle.py",
);

/** Returns null (extension inactive) for a missing/invalid file or missing `triage_dirs`. */
export function loadConfig(path: string = CONFIG_PATH): TriageConfig | null {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null) return null;
  const { triage_dirs, lifecycle } = raw as { triage_dirs?: unknown; lifecycle?: unknown };
  if (!Array.isArray(triage_dirs)) return null;
  const triageDirs = triage_dirs.filter((d): d is string => typeof d === "string");
  if (triageDirs.length === 0) return null;
  return {
    triageDirs,
    lifecycle: typeof lifecycle === "string" && lifecycle !== "" ? lifecycle : DEFAULT_LIFECYCLE,
  };
}

/** realpath that tolerates a missing tail: resolves the nearest existing ancestor. */
function realpathLoose(p: string): string {
  const abs = resolve(p);
  const tail: string[] = [];
  let cur = abs;
  for (;;) {
    try {
      return join(realpathSync(cur), ...tail.reverse());
    } catch {
      const parent = dirname(cur);
      if (parent === cur) return abs;
      tail.push(basename(cur));
      cur = parent;
    }
  }
}

export function isActiveDir(cwd: string, dirs: readonly string[]): boolean {
  if (typeof cwd !== "string" || cwd === "") return false;
  const real = realpathLoose(cwd);
  for (const d of dirs) {
    if (typeof d !== "string" || !isAbsolute(d)) continue;
    const rel = relative(realpathLoose(d), real);
    if (rel === "" || (rel !== ".." && !rel.startsWith("../") && !isAbsolute(rel))) {
      return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------- tokenizer

const SEPARATORS: Record<string, true> = {
  ";": true,
  "&": true,
  "|": true,
  "\n": true,
  "(": true,
  ")": true,
  "`": true,
};

/** Splits a command line into simple commands, each a list of unquoted words. */
export function splitCommands(command: string): string[][] {
  const segments: string[][] = [];
  let words: string[] = [];
  let cur = "";
  let started = false;
  let quote: "'" | '"' | null = null;

  const endWord = () => {
    if (started) words.push(cur);
    cur = "";
    started = false;
  };
  const endSegment = () => {
    endWord();
    if (words.length > 0) segments.push(words);
    words = [];
  };

  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote === "'") {
      if (c === "'") quote = null;
      else cur += c;
    } else if (quote === '"') {
      if (c === '"') {
        quote = null;
      } else if (c === "\\" && i + 1 < command.length) {
        const n = command[++i];
        cur += n === '"' || n === "\\" || n === "$" || n === "`" ? n : `\\${n}`;
      } else {
        cur += c;
      }
    } else if (c === "'" || c === '"') {
      quote = c;
      started = true;
    } else if (c === "\\") {
      if (i + 1 < command.length) {
        const n = command[++i];
        if (n !== "\n") {
          cur += n;
          started = true;
        }
      }
    } else if (c === " " || c === "\t" || c === "\r") {
      endWord();
    } else if (Object.hasOwn(SEPARATORS, c)) {
      endSegment();
    } else {
      cur += c;
      started = true;
    }
  }
  endSegment();
  return segments;
}

// ------------------------------------------------------------------ policy

const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
const SHELLS: Record<string, true> = { sh: true, bash: true, zsh: true, dash: true, ksh: true };
const ORCA_BINARIES: Record<string, true> = { orca: true, "orca-dev": true, "orca-ide": true };
/** Shell keywords that may precede a command word. */
const RESERVED: Record<string, true> = {
  "{": true,
  "!": true,
  if: true,
  then: true,
  else: true,
  elif: true,
  do: true,
  while: true,
  until: true,
  time: true,
};
/** Wrappers that run the following command; value = flags that consume an argument. */
const WRAPPERS: Record<string, readonly string[]> = {
  sudo: ["-u", "-g", "-h", "-p", "-C", "-U"],
  env: ["-u", "-C", "-S"],
  nice: ["-n"],
  command: [],
  builtin: [],
  exec: [],
  nohup: [],
};
/** git options that take a separate value argument. */
const GIT_VALUE_OPTIONS = ["-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"];

const MERGE_REASON =
  "git merge is blocked in the Orca triage chat: a merge needs a recorded human approval. " +
  "Use `python3 orca-lifecycle.py merge --task <t>` (or `python3 orca-lifecycle.py accept --task <t>` for report tasks).";
const START_REASON =
  "Starting workers directly bypasses the lifecycle registry. " +
  "Use `python3 orca-lifecycle.py start-worker ...` instead.";
const CHECK_REASON =
  "`orca orchestration check` consumes worker events without recording them. " +
  "Use `python3 orca-lifecycle.py wait` (read-only alternatives: `check --peek` or `check --all`).";
const REPLY_REASON =
  "Replying to a worker directly bypasses the registry. " +
  "Use `python3 orca-lifecycle.py answer --question <id> --body ...` instead.";
const GATE_REASON =
  "Resolving a gate directly skips the human approval record. " +
  "Use `python3 orca-lifecycle.py approve ...` or `reject ...` for merge gates, `python3 orca-lifecycle.py gate ...` for other gates.";
const COMPLETE_REASON =
  "Never mark a Task completed by hand: worker_done settles Tasks, and the registry records the rest.";

const MAX_DEPTH = 4;

/** Looks up `alias.<name>` in git config; returns null when there is none. */
export type AliasResolver = (name: string) => string | null;

function checkGit(args: readonly string[], depth: number, resolveAlias?: AliasResolver): string | null {
  if (depth > MAX_DEPTH) return null;
  const inlineAliases: Record<string, string> = {};
  let i = 0;
  while (i < args.length && args[i].startsWith("-")) {
    const opt = args[i];
    const value = opt === "-c" ? args[i + 1] : opt.startsWith("-c") ? opt.slice(2) : undefined;
    const m = value === undefined ? null : /^alias\.([^=]+)=(.*)$/s.exec(value);
    if (m) inlineAliases[m[1]] = m[2];
    i += GIT_VALUE_OPTIONS.includes(opt) ? 2 : 1;
  }
  const sub = args[i];
  if (sub === undefined) return null;
  const rest = args.slice(i + 1);
  if (sub === "merge") return rest.includes("--abort") ? null : MERGE_REASON;
  // An alias (`git -c alias.m=merge m x`, or `alias.m` in git config) can expand to merge.
  const expansion = Object.hasOwn(inlineAliases, sub) ? inlineAliases[sub] : resolveAlias?.(sub) ?? null;
  if (expansion === null) return null;
  if (expansion.startsWith("!")) return blockReasonAt(expansion.slice(1), depth + 1, resolveAlias);
  const words = splitCommands(expansion)[0] ?? [];
  return checkGit([...words, ...rest], depth + 1, resolveAlias);
}

function checkOrca(args: readonly string[]): string | null {
  const at = args.indexOf("orchestration");
  if (at === -1) return null;
  const rest = args.slice(at + 1);
  const verb = rest.find((a) => !a.startsWith("-"));
  switch (verb) {
    case "worker-start":
    case "dispatch":
      return START_REASON;
    case "check":
      return rest.some((a) => /^--(peek|all)(=|$)/.test(a)) ? null : CHECK_REASON;
    case "reply":
      return REPLY_REASON;
    case "gate-resolve":
      return GATE_REASON;
    case "task-update": {
      const i = rest.indexOf("--status");
      const completed =
        rest.includes("--status=completed") || (i !== -1 && rest[i + 1] === "completed");
      return completed ? COMPLETE_REASON : null;
    }
    default:
      return null;
  }
}

function checkSegment(words: readonly string[], depth: number, resolveAlias?: AliasResolver): string | null {
  let i = 0;
  for (;;) {
    const w = words[i];
    if (w === undefined) return null;
    if (ASSIGNMENT.test(w) || Object.hasOwn(RESERVED, w)) {
      i++;
      continue;
    }
    const wrapper = basename(w);
    if (!Object.hasOwn(WRAPPERS, wrapper)) break;
    const valueFlags = WRAPPERS[wrapper];
    i++;
    while (i < words.length && (words[i].startsWith("-") || ASSIGNMENT.test(words[i]))) {
      i += valueFlags.includes(words[i]) ? 2 : 1;
    }
  }

  const exe = basename(words[i]);
  const args = words.slice(i + 1);

  if (exe === "git") return checkGit(args, depth, resolveAlias);
  if (Object.hasOwn(ORCA_BINARIES, exe)) return checkOrca(args);
  if (Object.hasOwn(SHELLS, exe)) {
    const c = args.findIndex((a) => /^-[A-Za-z]*c[A-Za-z]*$/.test(a));
    const script = c === -1 ? undefined : args[c + 1];
    return script === undefined ? null : blockReasonAt(script, depth + 1, resolveAlias);
  }
  if (exe === "eval") return blockReasonAt(args.join(" "), depth + 1, resolveAlias);
  return null;
}

const SUBSTITUTION = /\$\(([^()]*)\)|`([^`]*)`/g;

function blockReasonAt(command: string, depth: number, resolveAlias?: AliasResolver): string | null {
  if (depth > MAX_DEPTH || command.trim() === "") return null;
  for (const words of splitCommands(command)) {
    const reason = checkSegment(words, depth, resolveAlias);
    if (reason !== null) return reason;
  }
  // Substitutions inside double quotes are not split out by the tokenizer.
  for (const m of command.matchAll(SUBSTITUTION)) {
    const reason = blockReasonAt(m[1] ?? m[2] ?? "", depth + 1, resolveAlias);
    if (reason !== null) return reason;
  }
  return null;
}

/** Reason text naming the replacement command, or null when the command is allowed. */
export function blockReason(command: string, resolveAlias?: AliasResolver): string | null {
  return blockReasonAt(command, 0, resolveAlias);
}

// ------------------------------------------------------------- file edits

const PIPELINE =
  "Small change: `python3 orca-lifecycle.py start-worker --spec ... --title ... --project <id> --run <run> " +
  "--acceptance ... --class small --agent omp`. Larger work: cut tickets with the to-tickets skill into " +
  ".scratch/<feature>/issues/ (Status: ready-for-agent, Blocked by: ...), then `import-tickets --dir " +
  ".scratch/<feature> --run <run> --project <id>` and `start-worker --task <id> --class large --agent omp` " +
  "per frontier ticket.";

const EDIT_REASON =
  "The triage chat coordinates; it does not change code, in this repo or any other. Code changes run in a " +
  "worker worktree. " + PIPELINE + " The triage chat may edit only Markdown files (SoT docs, decisions.md, tickets).";

const TASK_REASON =
  "The triage chat does not run implementation subagents: they would change code outside a worker worktree. " +
  PIPELINE + " Read-only subagents (scout, reviewer, council-strategist, improve-*) stay allowed.";

const EDITABLE = /\.(md|markdown)$/i;

/** Subagents that only read and report. Anything else may change files. */
const READ_ONLY_AGENTS: Record<string, true> = {
  scout: true, reviewer: true, "council-strategist": true, "improve-scout": true, "improve-critic": true,
  "improve-researcher": true, seance: true, "security-reviewer": true,
};

/** Paths a file-edit tool call would change. Unknown tools return []. */
export function editTargets(toolName: string, input: unknown): string[] {
  if (toolName !== "edit" && toolName !== "write") return [];
  if (typeof input !== "object" || input === null) return [];
  const paths: string[] = [];
  for (const key of ["path", "file_path", "filePath"]) {
    const value = key in input ? Reflect.get(input, key) : undefined;
    if (typeof value === "string") paths.push(value);
  }
  const patch = "input" in input ? input.input : undefined;
  if (toolName === "edit" && typeof patch === "string") {
    for (const m of patch.matchAll(/^\[(.+?)#[0-9A-Fa-f]{4}\]\s*$/gm)) paths.push(m[1]);
  }
  return paths;
}

/** Reason text when the triage chat would edit a non-Markdown file, wherever it lives. */
export function editBlockReason(toolName: string, input: unknown): string | null {
  for (const p of editTargets(toolName, input)) {
    if (p === "xd://ast_edit") return EDIT_REASON; // structural code rewrite
    if (p.includes("://")) continue; // internal URLs (local://, agent://, ...) are not project files
    if (!EDITABLE.test(p)) return EDIT_REASON;
  }
  return null;
}

/** Reason text when a `task` call would start a subagent that can change files. */
export function taskBlockReason(input: unknown): string | null {
  const tasks = typeof input === "object" && input !== null && "tasks" in input ? input.tasks : undefined;
  if (!Array.isArray(tasks)) return null;
  for (const t of tasks) {
    const agent = typeof t === "object" && t !== null && "agent" in t ? t.agent : undefined;
    const name = typeof agent === "string" && agent !== "" ? agent : "task";
    if (!Object.hasOwn(READ_ONLY_AGENTS, name)) return TASK_REASON;
  }
  return null;
}
