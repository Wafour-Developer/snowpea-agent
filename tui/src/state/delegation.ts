/**
 * `$reviewer look at this diff` — the short way to hand one prompt to an agent.
 *
 * The daemon parses the prefix itself; this is only the part that recognises it
 * while it is being typed, so the input can say which agent is about to get the
 * work before the prompt is sent. An unknown name is still shown, marked as
 * unknown, rather than silently ignored: the mistake is worth seeing before
 * pressing Enter, not after.
 *
 * Pure, so `test/delegation.test.ts` can check the parsing.
 */

/** What a draft beginning with `$name ` is addressed to. */
export interface DelegationHint {
  /** The agent named after the `$`. */
  name: string;
  /** True when `agent.list` knows the name. */
  known: boolean;
  /** The prompt without the prefix, for the chip. */
  rest: string;
}

const PREFIX = /^\$([A-Za-z0-9][\w.-]*)(\s+([\s\S]*))?$/;

/**
 * Read the delegation prefix out of a draft, or null when there is none.
 *
 * The name has to be followed by a space before it counts, so typing `$` and
 * then a name does not flicker a hint on every keystroke of the name itself.
 */
export function delegationHint(draft: string, agents: readonly string[] = []): DelegationHint | null {
  const match = PREFIX.exec(draft);
  if (!match) return null;
  const [, name, spaced, rest] = match;
  if (!spaced) return null;
  return {
    name,
    known: agents.includes(name),
    rest: (rest ?? "").trim(),
  };
}

/** `delegate to reviewer`, or the same with a warning when nothing is by that name. */
export function delegationLabel(hint: DelegationHint): string {
  return hint.known ? `delegate to ${hint.name}` : `delegate to ${hint.name} (no such agent)`;
}
