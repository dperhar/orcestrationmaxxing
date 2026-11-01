import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import {
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { blockReason, editBlockReason, isActiveDir, loadConfig, taskBlockReason } from "./policy";

describe("blockReason: git merge", () => {
  test("blocks plain git merge and names the replacement", () => {
    const r = blockReason("git merge feature/x");
    expect(r).not.toBeNull();
    expect(r).toContain("orca-lifecycle.py merge --task");
    expect(r).toContain("accept");
  });

  test("blocks merge flags and git global options before the subcommand", () => {
    expect(blockReason("git merge --no-ff feature/x")).not.toBeNull();
    expect(blockReason("git -C /tmp/repo merge feature/x")).not.toBeNull();
    expect(blockReason("git -c user.name=x merge feature/x")).not.toBeNull();
    expect(blockReason("git --no-pager merge feature/x")).not.toBeNull();
  });

  test("allows git merge --abort", () => {
    expect(blockReason("git merge --abort")).toBeNull();
    expect(blockReason("git -C /tmp/repo merge --abort")).toBeNull();
  });

  test("allows other git subcommands that merely contain the word", () => {
    expect(blockReason("git merge-base main HEAD")).toBeNull();
    expect(blockReason("git status")).toBeNull();
    expect(blockReason("git log --merges")).toBeNull();
    expect(blockReason('git commit -m "git merge later"')).toBeNull();
    expect(blockReason("echo git merge x")).toBeNull();
  });

  test("blocks inside chained commands", () => {
    expect(blockReason("cd x && git merge y")).not.toBeNull();
    expect(blockReason("cd x; git merge y")).not.toBeNull();
    expect(blockReason("false || git merge y")).not.toBeNull();
    expect(blockReason("echo hi | git merge y")).not.toBeNull();
    expect(blockReason("cd x\ngit merge y")).not.toBeNull();
    expect(blockReason("(cd x && git merge y)")).not.toBeNull();
    expect(blockReason("cd x && git merge --abort")).toBeNull();
  });

  test("blocks wrapped and quoted invocations", () => {
    expect(blockReason("FOO=1 git merge y")).not.toBeNull();
    expect(blockReason("sudo git merge y")).not.toBeNull();
    expect(blockReason("env GIT_X=1 git merge y")).not.toBeNull();
    expect(blockReason('git "merge" y')).not.toBeNull();
    expect(blockReason("/usr/bin/git merge y")).not.toBeNull();
    expect(blockReason("bash -c 'cd x && git merge y'")).not.toBeNull();
    expect(blockReason('sh -lc "git merge y"')).not.toBeNull();
    expect(blockReason('eval "git merge y"')).not.toBeNull();
    expect(blockReason("echo $(git merge y)")).not.toBeNull();
    expect(blockReason('echo "$(git merge y)"')).not.toBeNull();
    expect(blockReason("echo `git merge y`")).not.toBeNull();
  });
});

describe("blockReason: orca orchestration", () => {
  test("blocks worker-start and dispatch -> start-worker", () => {
    for (const verb of ["worker-start", "dispatch"]) {
      const r = blockReason(`orca orchestration ${verb} --task t1`);
      expect(r).not.toBeNull();
      expect(r).toContain("orca-lifecycle.py start-worker");
    }
  });

  test("blocks check without --peek/--all -> wait", () => {
    const r = blockReason("orca orchestration check --json");
    expect(r).not.toBeNull();
    expect(r).toContain("orca-lifecycle.py wait");
    expect(blockReason("orca orchestration check")).not.toBeNull();
  });

  test("allows check --peek and check --all", () => {
    expect(blockReason("orca orchestration check --peek")).toBeNull();
    expect(blockReason("orca orchestration check --json --peek")).toBeNull();
    expect(blockReason("orca orchestration check --all")).toBeNull();
  });

  test("blocks reply -> answer", () => {
    const r = blockReason("orca orchestration reply --question q1 --body hi");
    expect(r).not.toBeNull();
    expect(r).toContain("orca-lifecycle.py answer --question");
  });

  test("blocks gate-resolve -> approve|reject", () => {
    const r = blockReason("orca orchestration gate-resolve --gate g1");
    expect(r).not.toBeNull();
    expect(r).toContain("orca-lifecycle.py approve");
    expect(r).toContain("reject");
  });

  test("blocks task-update --status completed only", () => {
    const r = blockReason("orca orchestration task-update --id t1 --status completed");
    expect(r).not.toBeNull();
    expect(r).toContain("worker_done");
    expect(blockReason("orca orchestration task-update --id t1 --status=completed")).not.toBeNull();
    expect(blockReason("orca orchestration task-update --id t1 --status in_progress")).toBeNull();
    expect(blockReason("orca orchestration task-update --id t1 --title completed")).toBeNull();
  });

  test("allows read-only verbs", () => {
    for (const cmd of [
      "orca orchestration task-list",
      "orca orchestration gate-list",
      "orca orchestration worker-list",
      "orca orchestration worker-show --id w1",
      "orca orchestration worker-read --id w1",
      "orca orchestration status",
      "orca orchestration inbox",
      "orca status",
      "orca worktree ps",
    ]) {
      expect(blockReason(cmd)).toBeNull();
    }
  });

  test("matches orca-dev, orca-ide and absolute paths ending in /orca", () => {
    expect(blockReason("orca-dev orchestration dispatch --task t")).not.toBeNull();
    expect(blockReason("orca-ide orchestration reply --question q")).not.toBeNull();
    expect(
      blockReason("/Applications/Orca.app/Contents/Resources/bin/orca orchestration worker-start"),
    ).not.toBeNull();
    expect(blockReason("'/opt/my tools/orca' orchestration gate-resolve --gate g")).not.toBeNull();
    expect(blockReason('"/opt/x/orca" orchestration check')).not.toBeNull();
  });

  test("does not mistake other executables for orca", () => {
    expect(blockReason("orcastrate orchestration dispatch")).toBeNull();
    expect(blockReason("/opt/orca-tools/other orchestration dispatch")).toBeNull();
    expect(blockReason("echo orca orchestration dispatch")).toBeNull();
  });

  test("blocks inside chained commands and wrappers", () => {
    expect(blockReason("cd x && orca orchestration dispatch --task t")).not.toBeNull();
    expect(blockReason("ORCA_X=1 orca orchestration reply --question q")).not.toBeNull();
    expect(blockReason("orca --json orchestration dispatch --task t")).not.toBeNull();
    expect(blockReason("bash -c 'orca orchestration gate-resolve --gate g'")).not.toBeNull();
    expect(blockReason("orca orchestration check --peek && orca orchestration reply --question q")).not.toBeNull();
  });
});

describe("blockReason: lifecycle registry stays allowed", () => {
  test("orca-lifecycle.py commands are never blocked", () => {
    for (const cmd of [
      "python3 orca-lifecycle.py merge --task t1",
      "/usr/bin/python3 ~/Library/Scripts/orcestrationmaxxing/orca-lifecycle.py start-worker --task t1",
      "python3 orca-lifecycle.py wait",
      "python3 orca-lifecycle.py answer --question q1 --body 'git merge it'",
      "python3 orca-lifecycle.py approve --task t1",
      "~/Library/Scripts/orcestrationmaxxing/orca-lifecycle.py reload",
    ]) {
      expect(blockReason(cmd)).toBeNull();
    }
  });

  test("empty, blank, and garbage input is allowed", () => {
    expect(blockReason("")).toBeNull();
    expect(blockReason("   ")).toBeNull();
    expect(blockReason("echo 'unterminated")).toBeNull();
    expect(blockReason("ls -la")).toBeNull();
  });
});

describe("isActiveDir", () => {
  let root: string;
  let triage: string;
  let link: string;

  beforeAll(() => {
    root = realpathSync(mkdtempSync(join(tmpdir(), "orca-triage-")));
    triage = join(root, "triage");
    mkdirSync(join(triage, "sub", "deep"), { recursive: true });
    mkdirSync(join(root, "triage-sibling"));
    link = join(root, "link");
    symlinkSync(triage, link);
  });

  afterAll(() => rmSync(root, { recursive: true, force: true }));

  test("active when equal, inside, or reached through a symlink", () => {
    expect(isActiveDir(triage, [triage])).toBe(true);
    expect(isActiveDir(join(triage, "sub", "deep"), [triage])).toBe(true);
    expect(isActiveDir(link, [triage])).toBe(true);
  });

  test("inactive for siblings sharing a name prefix, parents, and unrelated dirs", () => {
    expect(isActiveDir(join(root, "triage-sibling"), [triage])).toBe(false);
    expect(isActiveDir(root, [triage])).toBe(false);
    expect(isActiveDir(tmpdir(), [triage])).toBe(false);
  });

  test("inactive for empty list, empty cwd, relative or non-string entries", () => {
    expect(isActiveDir(triage, [])).toBe(false);
    expect(isActiveDir("", [triage])).toBe(false);
    expect(isActiveDir(triage, ["triage", "", 5 as unknown as string])).toBe(false);
  });

  test("does not throw for non-existent cwd", () => {
    expect(isActiveDir(join(triage, "missing", "x"), [triage])).toBe(true);
    expect(isActiveDir(join(root, "nope"), [triage])).toBe(false);
  });
});

describe("loadConfig", () => {
  let dir: string;

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "orca-triage-cfg-"));
  });
  afterAll(() => rmSync(dir, { recursive: true, force: true }));

  const write = (name: string, body: string): string => {
    const p = join(dir, name);
    writeFileSync(p, body);
    return p;
  };

  test("reads triage_dirs and lifecycle", () => {
    const p = write("ok.json", JSON.stringify({ triage_dirs: ["/a", "/b"], lifecycle: "/x/orca-lifecycle.py" }));
    expect(loadConfig(p)).toEqual({ triageDirs: ["/a", "/b"], lifecycle: "/x/orca-lifecycle.py" });
  });

  test("defaults lifecycle when absent", () => {
    const p = write("nolife.json", JSON.stringify({ triage_dirs: ["/a"] }));
    expect(loadConfig(p)).toEqual({
      triageDirs: ["/a"],
      lifecycle: join(homedir(), "Library", "Scripts", "orcestrationmaxxing", "orca-lifecycle.py"),
    });
  });

  test("returns null for missing file, bad JSON, missing or empty triage_dirs", () => {
    expect(loadConfig(join(dir, "does-not-exist.json"))).toBeNull();
    expect(loadConfig(write("bad.json", "{nope"))).toBeNull();
    expect(loadConfig(write("nokey.json", JSON.stringify({ lifecycle: "/x" })))).toBeNull();
    expect(loadConfig(write("empty.json", JSON.stringify({ triage_dirs: [] })))).toBeNull();
    expect(loadConfig(write("wrong.json", JSON.stringify({ triage_dirs: "/a" })))).toBeNull();
    expect(loadConfig(write("arr.json", "[]"))).toBeNull();
  });

  test("drops non-string entries", () => {
    const p = write("mixed.json", JSON.stringify({ triage_dirs: ["/a", 3, null, "/b"] }));
    expect(loadConfig(p)?.triageDirs).toEqual(["/a", "/b"]);
  });
});

describe("blockReason: git aliases", () => {
  const config: Record<string, string> = { m: "merge --no-ff", sm: "!git merge", lg: "log --oneline" };
  const resolve = (name: string) => config[name] ?? null;

  test("blocks inline -c aliases that expand to merge", () => {
    expect(blockReason("git -c alias.m=merge m feature")).not.toBeNull();
    expect(blockReason("git -calias.m=merge m feature")).not.toBeNull();
    expect(blockReason("git -c alias.m='!git merge' m feature")).not.toBeNull();
  });

  test("blocks config aliases that expand to merge, plain or shell", () => {
    expect(blockReason("git m feature", resolve)).not.toBeNull();
    expect(blockReason("git sm feature", resolve)).not.toBeNull();
  });

  test("allows aliases that do not merge, and merge --abort through an alias", () => {
    expect(blockReason("git lg", resolve)).toBeNull();
    expect(blockReason("git m --abort", resolve)).toBeNull();
    expect(blockReason("git unknown-alias", resolve)).toBeNull();
  });
});

describe("editBlockReason: the triage chat never edits code", () => {
  test("blocks code anywhere and names the ticket pipeline", () => {
    const r = editBlockReason("write", { path: "app/main.py" });
    expect(r).toContain("start-worker");
    expect(r).toContain("to-tickets");
    expect(editBlockReason("write", { path: "/Users/x/other-repo/src/router.ts" })).not.toBeNull();
    expect(editBlockReason("edit", { input: "[app/main.py#1A2B]\nPUT 1.=1:\n+x" })).not.toBeNull();
    expect(editBlockReason("write", { path: "xd://ast_edit" })).not.toBeNull();
  });

  test("allows Markdown and internal URLs, ignores non-edit tools", () => {
    expect(editBlockReason("write", { path: "ARCHITECTURE.md" })).toBeNull();
    expect(editBlockReason("edit", { input: "[.scratch/feat/issues/01-x.md#1A2B]\nPUT 1.=1:\n+x" })).toBeNull();
    expect(editBlockReason("write", { path: "local://plan.md" })).toBeNull();
    expect(editBlockReason("read", { path: "app/main.py" })).toBeNull();
  });

  test("blocks a multi-file edit when any target is code", () => {
    const input = "[notes.md#1A2B]\nPUT 1.=1:\n+x\n[app/x.py#3C4D]\nPUT 1.=1:\n+y";
    expect(editBlockReason("edit", { input })).not.toBeNull();
  });
});

describe("taskBlockReason: no implementation subagents in triage", () => {
  test("blocks code-implementer and the default task agent", () => {
    expect(taskBlockReason({ tasks: [{ agent: "code-implementer", task: "x" }] })).toContain("worker worktree");
    expect(taskBlockReason({ tasks: [{ task: "x" }] })).not.toBeNull();
    expect(taskBlockReason({ tasks: [{ agent: "reviewer" }, { agent: "code-implementer" }] })).not.toBeNull();
  });

  test("allows read-only roles", () => {
    expect(taskBlockReason({ tasks: [{ agent: "reviewer" }, { agent: "council-strategist" }, { agent: "scout" }] })).toBeNull();
    expect(taskBlockReason({ tasks: [{ agent: "improve-critic" }] })).toBeNull();
    expect(taskBlockReason({})).toBeNull();
  });
});
