/**
 * OSC 7501 status (design m37 §2.3 / §2.9).
 *
 * `ESC ] 7501 ; key=val(:key=val)* BEL` tells a terminal host (snowpea's own
 * terminal sessions) what this TUI is doing. Plain terminals are left alone:
 * the emitter is off unless the TUI runs on a TTY and was asked to emit, by
 * `SNOWPEA_TERMINAL_ID` (set by the terminal host) or `SNOWPEA_OSC_STATUS=1`.
 */

export type OscState = "idle" | "working" | "done" | "blocked" | "clear";
export type OscKind = "permission" | "question";

export interface StatusInput {
  turnActive: boolean;
  /** An approval is open. */
  permissionPending: boolean;
  /** A question is open. */
  questionPending: boolean;
}

export interface OscStatus {
  state: OscState;
  kind?: OscKind;
}

/** Values must not carry the grammar's own separators or control bytes. */
function clean(value: string): string {
  return value.replace(/[^A-Za-z0-9._\-]/g, "_");
}

/** The status the TUI is in, given what it shows. `after` is the previous one. */
export function deriveStatus(input: StatusInput, after: OscStatus | null): OscStatus {
  if (input.permissionPending) return { state: "blocked", kind: "permission" };
  if (input.questionPending) return { state: "blocked", kind: "question" };
  if (input.turnActive) return { state: "working" };
  // A turn that just ended is "done"; it stays done until the next turn starts.
  if (after && (after.state === "working" || after.state === "blocked" || after.state === "done")) {
    return { state: "done" };
  }
  return { state: "idle" };
}

/**
 * The TUI's own status is the terminal's root record, so it carries no `id`:
 * a record with an `id` is a sub-record the core never ranks as the terminal's
 * status (the TUI's blocked dialog went unseen; security review H4).
 */
export function oscSequence(status: OscStatus): string {
  const parts = [`state=${clean(status.state)}`];
  if (status.kind) parts.push(`kind=${clean(status.kind)}`);
  parts.push("app=snowpea");
  return `\u001b]7501;${parts.join(":")}\u0007`;
}

export function oscEnabled(
  stdout: { isTTY?: boolean },
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  if (!stdout.isTTY) return false;
  return Boolean(env.SNOWPEA_TERMINAL_ID) || env.SNOWPEA_OSC_STATUS === "1";
}

/** Writes a sequence only when the status changed; a no-op when disabled. */
export class OscEmitter {
  private last: OscStatus | null = null;

  constructor(
    private readonly write: (chunk: string) => void,
    private readonly enabled: boolean,
  ) {}

  update(input: StatusInput): void {
    if (!this.enabled) return;
    const next = deriveStatus(input, this.last);
    if (this.last && this.last.state === next.state && this.last.kind === next.kind) return;
    this.last = next;
    this.write(oscSequence(next));
  }

  /** `state=clear` on exit; safe to call twice. */
  clear(): void {
    if (!this.enabled || this.last?.state === "clear") return;
    this.last = { state: "clear" };
    this.write(oscSequence(this.last));
  }
}
