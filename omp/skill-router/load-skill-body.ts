import { readFileSync } from "node:fs";
import {
  INLINE_BODY_MAX_CHARS,
  loadPathFor,
  registryByName,
  shouldInlineSkillBody,
} from "./registry";

export type SkillBodyPayload = {
  name: string;
  loadPath: string;
  mode: "inline" | "must_read_path";
  body?: string;
  chars?: number;
};

/**
 * Resolve how to put a skill in front of the model:
 * - small/medium skills: inline full SKILL.md into the directive (guaranteed summon)
 * - huge skills (autoplan etc.): path-only must_read (model must read file)
 */
export function resolveSkillPayload(
  name: string,
  explicitLoadPath?: string,
): SkillBodyPayload {
  const loadPath = explicitLoadPath || loadPathFor(name);
  const row = registryByName(name);
  const chars = row?.chars ?? 0;
  const filePath = skillFilePath(row?.sourcePath, loadPath);

  if (!filePath || (!explicitLoadPath && !shouldInlineSkillBody(name))) {
    return { name, loadPath, mode: "must_read_path", chars };
  }

  try {
    const body = readFileSync(filePath, "utf8");
    if (body.length > INLINE_BODY_MAX_CHARS) {
      return {
        name,
        loadPath: filePath,
        mode: "must_read_path",
        chars: body.length,
      };
    }
    return {
      name,
      loadPath: filePath,
      mode: "inline",
      body,
      chars: body.length,
    };
  } catch {
    return { name, loadPath, mode: "must_read_path", chars };
  }
}

export function resolvePrimaryPayloads(
  primary: string,
  mustRead: string[],
): SkillBodyPayload[] {
  const explicitLoadPaths = new Map<string, string>();
  for (const m of mustRead) {
    const n = skillNameFromMustRead(m);
    if (n && isSkillFilePath(m)) explicitLoadPaths.set(n, m);
  }

  const names: string[] = [];
  if (primary) names.push(primary);
  for (const m of mustRead) {
    const n = skillNameFromMustRead(m);
    if (n && !names.includes(n)) names.push(n);
  }
  // never inline work-routing twice noise — still include once
  return names.map((n) => resolveSkillPayload(n, explicitLoadPaths.get(n)));
}

function skillNameFromMustRead(item: string): string {
  if (item.startsWith("skill://")) return item.slice("skill://".length);
  const m = item.match(/\/([^/]+)\/SKILL\.md$/);
  if (m) return m[1];
  // bare name
  if (!item.includes("/") && !item.includes(" ")) return item;
  return "";
}

function skillFilePath(sourcePath: string | undefined, loadPath: string): string {
  if (sourcePath?.startsWith("/")) return sourcePath;
  if (isSkillFilePath(loadPath)) return loadPath;
  return "";
}

function isSkillFilePath(item: string): boolean {
  return !item.startsWith("skill://") && item.endsWith("/SKILL.md");
}
