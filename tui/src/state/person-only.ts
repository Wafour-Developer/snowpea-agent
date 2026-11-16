/**
 * What only a person may decide (1.54.0): an approval core marks `personOnly`
 * or `neverDelegable`, and a question that is `personOnly` or a mode picker
 * (`kind: "mode"`). Jarvis never decides these, so no surface shows them as
 * "Jarvis is deciding", whatever their `route` says.
 *
 * No imports of its own: the store, the Jarvis state and the prompts all read
 * it, and none of them may end up importing the others through it.
 */

/** The tag a request carries when Jarvis can never decide it. */
export const ALWAYS_ASKS = "always asks you";

export interface PersonOnlyFields {
  personOnly?: boolean | null;
  neverDelegable?: boolean | null;
  kind?: string | null;
}

/** True when only a person may decide this request. */
export function alwaysAsksYou(request: PersonOnlyFields | null | undefined): boolean {
  return request?.personOnly === true || request?.neverDelegable === true || request?.kind === "mode";
}

/** Why core put a request to the person whatever the mode (`reason`), when it said. */
export function alwaysAsksReason(request: { reason?: string | null } | null | undefined): string | null {
  const reason = typeof request?.reason === "string" ? request.reason.trim() : "";
  return reason === "" ? null : reason;
}
