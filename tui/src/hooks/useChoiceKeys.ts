/**
 * One key map for every choice the TUI puts in front of a person (M15b §2).
 *
 * Before this, each picker grew its own handler and they drifted: one moved
 * with Tab, another did not; one took `1`-`9`, another swallowed them; Esc
 * meant cancel here and nothing there. A person who learned the approval
 * prompt could not drive the model picker. So the map lives here once:
 *
 *   ↑ ↓ (and j / k)  move           Space   toggle, in multi-select
 *   Enter            pick / confirm 1-9     jump to row N, never submits
 *   ← →              previous / next tab    Esc     cancel
 *
 * Everything else is swallowed on purpose: a prompt that is up owns the
 * keyboard, and a stray character must never reach the chat draft behind it.
 */

import { useInput, type Key } from "ink";

export interface ChoiceKeyOptions {
  /** Rows the cursor can sit on. */
  count: number;
  /** Where the cursor is now. */
  index: number;
  /** Move the cursor. Always called with a value already inside `count`. */
  onIndex: (index: number) => void;
  /** Enter on the row the cursor is on. */
  onEnter?: (index: number) => void;
  /** Esc. */
  onCancel?: () => void;
  /** Space; only consulted when `multi` is set. */
  onToggle?: (index: number) => void;
  multi?: boolean;
  /**
   * `1`-`9`. Defaults to moving the cursor there — and toggling too, in
   * multi-select. It never submits: jumping to a row is not agreeing to it.
   */
  onDigit?: (index: number) => void;
  /** Rows `1`-`9` may reach; the option rows, when a list has trailing rows. */
  digitLimit?: number;
  /** Turn the digits off entirely (a list of one row does not need them). */
  digits?: boolean;
  /** ← and →, for tab strips and scope strips. */
  onLeft?: () => void;
  onRight?: () => void;
  /** Tab / Shift+Tab, when the surface gives them a meaning. */
  onTab?: () => void;
  onShiftTab?: () => void;
  /** Single letters, matched case-insensitively before anything else. */
  shortcuts?: Record<string, () => void>;
  /** j / k as ↓ / ↑; on by default, off where the letters mean something. */
  vim?: boolean;
  /** First chance at the key; return true to say it was handled. */
  onKey?: (input: string, key: Key) => boolean;
  isActive?: boolean;
}

/** Install the shared choice key map. */
export function useChoiceKeys(options: ChoiceKeyOptions): void {
  const {
    count,
    index,
    onIndex,
    onEnter,
    onCancel,
    onToggle,
    multi = false,
    onDigit,
    digitLimit,
    digits = true,
    onLeft,
    onRight,
    onTab,
    onShiftTab,
    shortcuts,
    vim = true,
    onKey,
    isActive = true,
  } = options;

  useInput(
    (input, key) => {
      if (onKey?.(input, key)) return;

      if (key.escape) {
        onCancel?.();
        return;
      }
      if (key.tab && key.shift) {
        if (onShiftTab) onShiftTab();
        return;
      }
      if (key.tab) {
        if (onTab) onTab();
        return;
      }
      if (key.leftArrow) {
        onLeft?.();
        return;
      }
      if (key.rightArrow) {
        onRight?.();
        return;
      }

      const typed = input.toLowerCase();
      const shortcut = shortcuts?.[typed];
      if (shortcut) {
        shortcut();
        return;
      }

      if (count > 0) {
        if (key.upArrow || (vim && typed === "k")) {
          onIndex((index + count - 1) % count);
          return;
        }
        if (key.downArrow || (vim && typed === "j")) {
          onIndex((index + 1) % count);
          return;
        }
      }

      if (input === " ") {
        // Space is the multi-select toggle and nothing else; in single-select
        // it is swallowed rather than treated as a second Enter.
        if (multi) onToggle?.(index);
        return;
      }

      if (key.return) {
        onEnter?.(index);
        return;
      }

      if (digits && /^[1-9]$/.test(input)) {
        const target = Number(input) - 1;
        const limit = digitLimit ?? count;
        if (target < limit) {
          if (onDigit) onDigit(target);
          else {
            onIndex(target);
            if (multi) onToggle?.(target);
          }
        }
        return;
      }
      // Anything else is swallowed.
    },
    { isActive },
  );
}

/** The footer hint, naming exactly the keys that are live (M15b §2). */
export function choiceHint(options: {
  multi?: boolean;
  tabs?: boolean;
  digits?: boolean;
  /** What Enter does here: "confirm", "pick", "save"… */
  enter?: string;
  /** Extra segments, e.g. "a all · n none". */
  extra?: string[];
  /** What Esc does here. */
  cancel?: string;
}): string {
  const {
    multi = false,
    tabs = false,
    digits = true,
    enter = "confirm",
    extra = [],
    cancel = "cancel",
  } = options;
  return [
    "↑↓ move",
    multi ? "Space toggle" : null,
    `Enter ${enter}`,
    digits ? (multi ? "1-9 toggle" : "1-9 pick") : null,
    tabs ? "←→ tabs" : null,
    ...extra,
    `Esc ${cancel}`,
  ]
    .filter(Boolean)
    .join(" · ");
}
