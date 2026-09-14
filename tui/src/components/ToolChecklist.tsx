/**
 * Which of a server's tools this session should register.
 *
 * An MCP server can arrive with forty tools, and every one of them costs the
 * model context on every turn. The daemon stores the answer as
 * `tools.include`; this is the place a person gives it — a list with a box in
 * front of each row rather than a comma-separated argument to remember.
 */

import React, { useState } from "react";
import { Box, Text } from "ink";

import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { McpTool } from "../state/mcp.js";
import { ChoiceList } from "./ChoiceList.js";

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

  const toggle = (row: number): void => {
    const tool = tools[row];
    if (!tool) return;
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(tool.name)) next.delete(tool.name);
      else next.add(tool.name);
      return next;
    });
  };

  useChoiceKeys({
    count: tools.length,
    index,
    onIndex: setIndex,
    multi: true,
    onToggle: toggle,
    onEnter: () => onSubmit(tools.map((tool) => tool.name).filter((name) => picked.has(name))),
    onCancel,
    onTab: () => {
      if (tools.length > 0) setIndex((i) => (i + 1) % tools.length);
    },
    // `a` and `n` are the two bulk answers this list has always had, so they
    // win over the vim motions rather than sharing the alphabet with them.
    shortcuts: {
      a: () => setPicked(new Set(tools.map((tool) => tool.name))),
      n: () => setPicked(new Set()),
    },
    vim: false,
    isActive,
  });

  const hint = choiceHint({
    multi: true,
    enter: "save",
    extra: ["a all", "n none"],
  });

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        {title}
      </Text>
      {tools.length === 0 ? (
        <>
          <Text dimColor>this server reported no tools</Text>
          <Text dimColor>{`0/0 chosen · ${hint}`}</Text>
        </>
      ) : (
        <ChoiceList
          options={tools.map((tool) => ({ label: tool.name, description: tool.description }))}
          selectedIndex={index}
          checked={
            new Set(
              tools
                .map((tool, at) => (picked.has(tool.name) ? at : -1))
                .filter((at) => at >= 0),
            )
          }
          multi
          color="green"
          windowSize={CHECKLIST_ROWS}
          descriptionMode="inline"
          hint={`${picked.size}/${tools.length} chosen · ${hint}`}
        />
      )}
    </Box>
  );
}
