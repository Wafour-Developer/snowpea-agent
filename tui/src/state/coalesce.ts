/**
 * Rate-limiting the child sessions' token stream.
 *
 * A delegate streams its answer the same way the main session does: one
 * `message.delta` per token, which for a fast endpoint is a hundred events a
 * second. The main transcript can afford that — it is one line growing at the
 * bottom — but a child's transcript is only ever on screen as the open agent
 * view, where each event costs a dispatch, a re-wrap of the child's whole
 * transcript and a full repaint of the live region. At a hundred a second the
 * terminal never finishes a frame before the next one starts, which is the
 * flicker, and `useInput` never gets a turn, which is the dead keyboard.
 *
 * So child deltas are throttled: the first one goes straight through, the ones
 * that follow inside the window are concatenated and delivered as a single
 * delta when it closes. Text is never dropped and never reordered — any other
 * kind of event flushes that session's pending text before it is delivered, so
 * a `message.done` can still close the very message the deltas were building.
 *
 * Pure except for the timer, which is injectable, so `test/coalesce.test.ts`
 * can drive it without waiting.
 */

/** The window child deltas are batched into: ~15 frames a second. */
export const CHILD_FLUSH_MS = 66;

/** The shape this module needs from a session event. */
export interface CoalescableEvent {
  sessionId?: string;
  seq?: number;
  kind?: string;
  payload?: unknown;
}

export interface ChildEventBuffer<E extends CoalescableEvent> {
  /** Take one child event; it is delivered now or at the end of the window. */
  push(sessionId: string, event: E): void;
  /** Deliver everything pending immediately. */
  flush(): void;
  /** Flush and stop the timer. */
  dispose(): void;
}

interface Pending<E> {
  /** The newest delta of the run, whose seq the merged event keeps. */
  event: E;
  text: string;
}

export interface BufferOptions {
  intervalMs?: number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: any) => void;
}

/**
 * The window the main session's counter events are batched into: 4 Hz.
 *
 * `message.reasoning` and `usage` carry no text anybody reads — they move the
 * "Thinking (n chars)…" line and the token counters. A long thinking block
 * emits one reasoning event per delta, and each one is a state change and a
 * repaint of the whole live region. Four a second is as fast as a number worth
 * reading changes.
 */
export const LIVE_FLUSH_MS = 250;

export interface LiveEventThrottle<E extends CoalescableEvent> {
  /** Take one main-session event; it is delivered now or when the window ends. */
  push(event: E): void;
  /** Deliver everything pending immediately. */
  flush(): void;
  /** Flush and stop the timer. */
  dispose(): void;
}

/** Number from a payload field, or 0. */
function count(event: CoalescableEvent, field: string): number {
  const value = (event.payload as Record<string, unknown> | undefined)?.[field];
  return typeof value === "number" ? value : Number(value ?? 0) || 0;
}

/**
 * How two events of the same kind combine, or null when this kind is not
 * batched at all.
 *
 * `message.reasoning` carries the running total, so the newest one says
 * everything the ones before it did. `usage` carries an increment, so the
 * batch has to add them up or tokens go missing from the counters.
 */
function mergeLive<E extends CoalescableEvent>(kind: string, older: E, newer: E): E | null {
  if (kind === "message.reasoning") return newer;
  if (kind === "usage") {
    return {
      ...newer,
      payload: {
        ...(newer.payload as object | undefined),
        inputTokens: count(older, "inputTokens") + count(newer, "inputTokens"),
        outputTokens: count(older, "outputTokens") + count(newer, "outputTokens"),
      },
    } as E;
  }
  return null;
}

/**
 * Rate-limit the main session's counter events without touching its text.
 *
 * `message.delta` deliberately does not go through this: the answer is what
 * the user is reading, and it must appear as it is written.
 */
export function createLiveEventThrottle<E extends CoalescableEvent>(
  deliver: (event: E) => void,
  { intervalMs = LIVE_FLUSH_MS, setTimer = setTimeout, clearTimer = clearTimeout }: BufferOptions = {},
): LiveEventThrottle<E> {
  const pending = new Map<string, E>();
  let timer: unknown = null;

  const drain = (): void => {
    for (const event of pending.values()) deliver(event);
    pending.clear();
  };

  const arm = (): void => {
    timer = setTimer(() => {
      timer = null;
      if (pending.size === 0) return;
      drain();
      arm();
    }, intervalMs);
  };

  return {
    push(event: E): void {
      const kind = String(event.kind ?? "");
      const batched = mergeLive(kind, event, event) !== null;
      if (!batched) {
        // Order before rate: a message or a tool call comes after the counters
        // that led up to it.
        drain();
        deliver(event);
        return;
      }
      const held = pending.get(kind);
      if (held) {
        pending.set(kind, mergeLive(kind, held, event) as E);
        return;
      }
      if (timer === null) {
        // Leading edge: the first "Thinking…" must appear at once.
        deliver(event);
        arm();
        return;
      }
      pending.set(kind, event);
    },
    flush(): void {
      drain();
    },
    dispose(): void {
      drain();
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}

function deltaText(event: CoalescableEvent): string | null {
  if (event.kind !== "message.delta") return null;
  const payload = event.payload as { text?: unknown } | undefined;
  const text = payload?.text;
  return typeof text === "string" ? text : "";
}

export function createChildEventBuffer<E extends CoalescableEvent>(
  deliver: (sessionId: string, event: E) => void,
  { intervalMs = CHILD_FLUSH_MS, setTimer = setTimeout, clearTimer = clearTimeout }: BufferOptions = {},
): ChildEventBuffer<E> {
  const pending = new Map<string, Pending<E>>();
  let timer: unknown = null;

  const merged = (entry: Pending<E>): E =>
    ({
      ...entry.event,
      payload: { ...(entry.event.payload as object | undefined), text: entry.text },
    }) as E;

  const drain = (): void => {
    for (const [sessionId, entry] of pending) deliver(sessionId, merged(entry));
    pending.clear();
  };

  const close = (): void => {
    if (timer !== null) clearTimer(timer);
    timer = null;
  };

  /** Open the window, or re-arm it while text keeps arriving. */
  const arm = (): void => {
    timer = setTimer(() => {
      timer = null;
      if (pending.size === 0) return;
      drain();
      // Still streaming, so keep the window open rather than letting the next
      // token through on the leading edge and doubling the frame rate.
      arm();
    }, intervalMs);
  };

  return {
    push(sessionId: string, event: E): void {
      const text = deltaText(event);
      if (text === null) {
        // Order before rate: whatever this is, it comes after the text so far.
        const entry = pending.get(sessionId);
        if (entry) {
          pending.delete(sessionId);
          deliver(sessionId, merged(entry));
        }
        deliver(sessionId, event);
        return;
      }
      const entry = pending.get(sessionId);
      if (entry) {
        entry.text += text;
        // The batch stands in for every delta in it, so it carries the newest
        // seq: the store's `lastSeq` must not go backwards.
        entry.event = event;
        return;
      }
      if (timer === null) {
        // Leading edge: the first token of a reply must not wait for a window.
        deliver(sessionId, event);
        arm();
        return;
      }
      pending.set(sessionId, { event, text });
    },
    flush(): void {
      drain();
    },
    dispose(): void {
      drain();
      close();
    },
  };
}
