export type WorkClass = "SMALL" | "LARGE" | "MIX" | "NON_BUILD";

/**
 * Who reads this turn's output. `internal` marks agent-to-agent traffic
 * (subagent dispatch), where reader-facing formatting is noise.
 */
export type OutputAudience = "user" | "internal";

export type EntryForm =
  | "hard_small"
  | "soft_grill"
  | "wayfinder"
  | "explicit_override"
  | "nmt"
  | "spp_pipeline"
  | "orchestration"
  | "research"
  | "bugfix"
  | "plan_review_gate"
  | "implement_ticket"
  | "intent_skill"
  | "degraded_fallback";

export type PlanGate = "none" | "autoplan_then_plan_review";

export interface RouteDecision {
  workClass: WorkClass;
  entryForm: EntryForm;
  primarySkill: string;
  mustReadSkills: string[];
  forbiddenSkills: string[];
  firstLine: string | null;
  planGate: PlanGate;
  qualityAdjuncts: string[];
  parkReminder: boolean;
  reason: string;
  activeScopeSummary: string;
  /**
   * Primary skill stays selected, but its body must not be inlined.
   * Set when a style ban applies to the primary itself (W1b).
   */
  suppressedPrimaryInline?: boolean;
  outputAudience?: OutputAudience;
}

export interface DecideRouteInput {
  prompt: string;
  hasActiveSppPipeline?: boolean;
  activeScopeSummary?: string;
  /** Optional ticket body/type when ticket session is known */
  ticketSoft?: boolean;
  ticketType?: string;
  outputAudience?: OutputAudience;
}

export interface SessionPrev {
  activeScopeSummary?: string;
  decision?: RouteDecision;
  /** Decision that established ACTIVE_SCOPE (park restores this). */
  scopeDecision?: RouteDecision;
  outputAudience?: OutputAudience;
}

export interface SessionRouteResult {
  activeScopeSummary: string;
  decision: RouteDecision;
  scopeDecision?: RouteDecision;
  outputAudience?: OutputAudience;
}
