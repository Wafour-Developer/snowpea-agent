/**
 * The one list-of-choices primitive (M15b §3).
 *
 * Every picker in the TUI — the approval prompt, `ask_user`, the model list,
 * the MCP catalog, the tool checklist, the little menus inside the add forms —
 * draws the same thing: rows with a cursor on one of them, a tick box when
 * several may be chosen, a dim line of explanation under a row that needs it,
 * a monospace preview beside the row when the option is easier shown than
 * told, and a footer hint that names the keys. Doing that eight times produced
 * eight slightly different lists, so it is done here once.
 *
 * The component draws; {@link useChoiceKeys} reads the keyboard. They are
 * separate because several callers (the question batch, the add forms) own
 * rows this list does not draw, and need the same key map anyway.
 */

import React from "react";
import { Box, Text } from "ink";

export interface ChoiceOption {
  /** What the row says. */
  label: string;
  /** One dim line under the row (or after it, in a dense list). */
  description?: string;
  /** Monospace block drawn beside the list while this row is under the cursor. */
  preview?: string;
  /** Single-key shortcut, drawn after the label as `(y)`. */
  shortcut?: string;
  /** Drawn in red; used for the refusing option. */
  danger?: boolean;
  /** Trailing marker, e.g. `← in use`. */
  badge?: string;
  /** Colour for {@link badge}. */
  badgeColor?: string;
  /** Draw the label bold even when the cursor is elsewhere. */
  bold?: boolean;
}

export interface ChoiceListProps {
  options: readonly ChoiceOption[];
  /** Row the cursor is on. */
  selectedIndex: number;
  /** Indices that are ticked; a set so a caller can keep its own order. */
  checked?: ReadonlySet<number>;
  /** Draw `[x]`/`[ ]` boxes instead of a bare row. */
  multi?: boolean;
  /**
   * Draw `●`/`○` in single-select, so a chosen row stays visible after the
   * cursor moves off it. Off for menus that act the moment Enter is pressed.
   */
  radio?: boolean;
  /** Number the rows `1.`, `2.`… so the digit keys are discoverable. */
  numbered?: boolean;
  /** Append the free-text row last (M15b §2). */
  allowOther?: boolean;
  /** What that row says. */
  otherLabel?: string;
  /** Text already typed into it, shown in place of the label. */
  otherText?: string | null;
  /** Cursor/active colour. */
  color?: string;
  /** Rows drawn at once; the list scrolls inside this. */
  windowSize?: number;
  /** `block` puts the description on its own indented line; `inline` after the label. */
  descriptionMode?: "block" | "inline";
  /** Footer hint. `null` draws none — for callers that write their own. */
  hint?: string | null;
  /** Draw the preview pane beside the list. On by default. */
  showPreview?: boolean;
}

/** Index of the `Other…` row, or -1 when the list has none. */
export function otherRowIndex(options: readonly unknown[], allowOther?: boolean): number {
  return allowOther ? options.length : -1;
}

/** First row of the window that keeps `index` visible without scrolling short lists. */
export function windowStart(index: number, total: number, size: number): number {
  if (total <= size) return 0;
  return Math.max(0, Math.min(index - size + 2, total - size));
}

export function ChoiceList({
  options,
  selectedIndex,
  checked,
  multi = false,
  radio = false,
  numbered = false,
  allowOther = false,
  otherLabel = "Other…",
  otherText = null,
  color = "cyan",
  windowSize,
  descriptionMode = "block",
  hint,
  showPreview = true,
}: ChoiceListProps): React.ReactElement {
  const size = windowSize ?? options.length;
  const start = windowStart(selectedIndex, options.length, Math.max(size, 1));
  const shown = options.slice(start, start + Math.max(size, 1));
  const preview = showPreview ? (options[selectedIndex]?.preview ?? "") : "";
  const other = otherRowIndex(options, allowOther);

  const row = (option: ChoiceOption, index: number): React.ReactElement => {
    const here = index === selectedIndex;
    const held = checked?.has(index) ?? false;
    const mark = multi ? (held ? "[x] " : "[ ] ") : radio ? (held ? "● " : "○ ") : "";
    const number = numbered ? `${index + 1}. ` : "";
    const rowColor = option.danger ? "red" : here ? color : undefined;
    return (
      <Box key={`${index}-${option.label}`} flexDirection="column">
        <Box>
          <Text color={here ? (option.danger ? "red" : color) : undefined} bold={here}>
            {here ? "❯ " : "  "}
          </Text>
          {/* With an inline description the two share one row. The label is what
              the user is choosing between, so it never gives up width: a long
              summary used to squeeze `/oh-my-claudecode:ralph` down to
              `/oh-my-claudecode:ra`, hiding the very part that was typed. */}
          <Box flexShrink={descriptionMode === "inline" ? 0 : 1}>
            <Text
              inverse={here}
              bold={here || option.bold}
              color={rowColor}
              dimColor={!here && !held && !option.danger}
              wrap="truncate-end"
            >
              {` ${number}${mark}${option.label} `}
            </Text>
          </Box>
          {option.shortcut ? <Text dimColor>{`  (${option.shortcut})`}</Text> : null}
          {option.description && descriptionMode === "inline" ? (
            <Text dimColor wrap="truncate-end">{`  ${option.description}`}</Text>
          ) : null}
          {option.badge ? (
            <Text color={option.badgeColor ?? color}>{`  ${option.badge}`}</Text>
          ) : null}
        </Box>
        {option.description && descriptionMode === "block" ? (
          <Text dimColor wrap="truncate-end">{`      ${option.description}`}</Text>
        ) : null}
      </Box>
    );
  };

  return (
    <Box flexDirection="column">
      <Box flexDirection="row">
        <Box flexDirection="column" flexGrow={1}>
          {shown.map((option, offset) => row(option, start + offset))}
          {allowOther ? (
            <Box>
              <Text color={selectedIndex === other ? color : undefined} bold={selectedIndex === other}>
                {selectedIndex === other ? "❯ " : "  "}
              </Text>
              <Text
                inverse={selectedIndex === other}
                color={selectedIndex === other ? color : undefined}
                dimColor={selectedIndex !== other && !otherText}
              >
                {` ${otherText ? `● ${otherText}` : otherLabel} `}
              </Text>
            </Box>
          ) : null}
        </Box>
        {preview ? (
          <Box
            flexDirection="column"
            marginLeft={2}
            borderStyle="single"
            borderColor="gray"
            paddingX={1}
          >
            {preview.split("\n").map((line, index) => (
              <Text key={`preview-${index}`} dimColor wrap="truncate-end">
                {line}
              </Text>
            ))}
          </Box>
        ) : null}
      </Box>
      {hint === null || hint === undefined ? null : <Text dimColor>{hint}</Text>}
    </Box>
  );
}
