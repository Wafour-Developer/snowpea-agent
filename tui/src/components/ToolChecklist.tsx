/**
 * Which of a server's tools this session should register.
 *
 * An MCP server can arrive with forty tools, and every one of them costs the
 * model context on every turn. The daemon stores the answer as
 * `tools.include`; this is the place a person gives it — a list with a box in
 * front of each row rather than a comma-separated argument to remember.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import type { McpTool } from "../state/mcp.js";

/** Rows shown at once; the list scrolls inside this. */
export const CHECKLIST_ROWS = 8;

export interface ToolChecklistProps {
  title: string;
  tools: readonly McpTool[];
  /** Tools ticked when the list opens; all of them when absent. */
  initial?: readonly string[];
  onSubmit: (names: string[]) => void;
  onCancel: () => void;
  isActive?: boolean;
  width: number;
}

export function ToolChecklist({
  title,
  tools,
  initial,
  onSubmit,
  onCancel,
  isActive = true,
  width,
}: ToolChecklistProps): React.ReactElement {
  const [index, setIndex] = useState(0);
  const [picked, setPicked] = useState<Set<string>>(
    () => new Set(initial && initial.length > 0 ? initial : tools.map((tool) => tool.name)),
  );

  useInput(
    (input, key) => {
      if (key.escape) {
        onCancel();
        return;
      }
      if (key.return) {
        onSubmit(tools.map((tool) => tool.name).filter((name) => picked.has(name)));
        return;
      }
      if (tools.length === 0) return;
      if (key.upArrow) {
        setIndex((i) => (i + tools.length - 1) % tools.length);
        return;
      }
      if (key.downArrow || key.tab) {
        setIndex((i) => (i + 1) % tools.length);
        return;
      }
      if (input === " ") {
        const name = tools[index].name;
        setPicked((current) => {
          const next = new Set(current);
          if (next.has(name)) next.delete(name);
          else next.add(name);
          return next;
        });
        return;
      }
      if (input === "a") {
        setPicked(new Set(tools.map((tool) => tool.name)));
        return;
      }
      if (input === "n") setPicked(new Set());
    },
    { isActive },
  );

  const start = Math.max(0, Math.min(index - CHECKLIST_ROWS + 2, tools.length - CHECKLIST_ROWS));
  const shown = tools.slice(start, start + CHECKLIST_ROWS);

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        {title}
      </Text>
      {tools.length === 0 ? (
        <Text dimColor>this server reported no tools</Text>
      ) : (
        shown.map((tool) => {
          const active = tools[index] === tool;
          return (
            <Text key={tool.name} wrap="truncate-end">
              <Text color={active ? "green" : undefined}>{active ? "❯ " : "  "}</Text>
              <Text color={picked.has(tool.name) ? "green" : undefined}>
                {picked.has(tool.name) ? "[x] " : "[ ] "}
              </Text>
              <Text inverse={active}>{tool.name}</Text>
              <Text dimColor>{tool.description ? `  ${tool.description}` : ""}</Text>
            </Text>
          );
        })
      )}
      <Text dimColor>{`${picked.size}/${tools.length} chosen · Space toggle · a all · n none · Enter save · Esc cancel`}</Text>
    </Box>
  );
}
