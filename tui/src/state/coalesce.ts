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
