/**
 * "That looked repeatable" — the nudge toward `/skill learn`.
 *
 * A turn that ran a lot of tools for a long time is the kind of work worth
 * keeping, and `/skill learn` is how it is kept. The nudge is shown once per
 * session and can be dismissed, because a hint that keeps coming back is an
 * advertisement.
 *
 * Pure, so `test/skill-hint.test.ts` can check the thresholds without a clock.
 */

/** Tool calls a turn has to have made before the hint is worth showing. */
export const SKILL_HINT_TOOL_CALLS = 6;

/** And how long it has to have run, so a fast burst of reads stays quiet. */
export const SKILL_HINT_MS = 30_000;

/** The line itself. */
export const SKILL_HINT_TEXT = "Turn this into a skill: /skill learn <name>";

export interface SkillHintInput {
  /** Tool calls this turn made. */
  toolCalls: number;
  /** How long the turn took. */
  elapsedMs: number;
  /** The turn ended cleanly; there is nothing reusable about a failure. */
  ok: boolean;
  /** The hint has already been shown this session. */
  shown: boolean;
  /** The user has waved it away. */
  dismissed: boolean;
}

/** Whether the turn that just ended deserves the nudge. */
export function shouldSuggestSkill({
  toolCalls,
  elapsedMs,
  ok,
  shown,
  dismissed,
}: SkillHintInput): boolean {
  if (shown || dismissed || !ok) return false;
  return toolCalls >= SKILL_HINT_TOOL_CALLS && elapsedMs >= SKILL_HINT_MS;
}
