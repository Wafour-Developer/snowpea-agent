/**
 * `/mcp add` with nothing after it: questions instead of a syntax.
 *
 * Typing the command in full means remembering where the `--` goes, which flag
 * carries an environment variable and what a scope is called. The form asks for
 * a name, a transport, the command line or the URL, the variables, and where
 * the entry should live — then probes the draft with `mcp.test` before anything
 * is written, the way M14 §3 requires: "Connected — N tools" and a choice
 * between all of them and a few of them.
 *
 * The daemon does every dangerous thing. This component never spawns anything:
 * the command line is split into argv here (`splitArgs`) and handed over as a
 * list, never as a string for a shell.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import {
  draftParams,
  isUnsafeError,
  isValidMcpName,
  maskAssignment,
  parseAssignment,
  probeSummary,
  splitArgs,
  type McpDraft,
  type McpProbe,
  type McpWritableScope,
} from "../state/mcp.js";
import { ToolChecklist } from "./ToolChecklist.js";

/** Which question is on screen. */
export type McpFormStep =
  | "name"
  | "transport"
  | "command"
  | "url"
  | "vars"
  | "scope"
  | "testing"
  | "connected"
  | "tools"
  | "failed"
  | "unsafe";

const TRANSPORTS: readonly { value: McpDraft["transport"]; label: string; hint: string }[] = [
  { value: "stdio", label: "Command", hint: "a program this machine runs, e.g. npx …" },
  { value: "url", label: "URL", hint: "a remote server, http or sse" },
];

const SCOPES: readonly { value: McpWritableScope; label: string; hint: string }[] = [
  { value: "project", label: "this project", hint: ".mcp.json — travels with the repo" },
  { value: "global", label: "everywhere", hint: "$SNOWPEA_HOME/.mcp.json — every project" },
];

/** What the form hands back once the user has confirmed the probe. */
export interface McpSubmission {
  /** `mcp.add` params, ready to send. */
  params: Record<string, unknown>;
  /** The tools to register, or null for all of them. */
  toolsInclude: string[] | null;
  /** True only after the user accepted a safety finding. */
  force: boolean;
}

export interface McpAddFormProps {
  /** Runs `mcp.test` on the draft; never throws — a failure is `ok: false`. */
  onTest: (params: Record<string, unknown>) => Promise<McpProbe>;
  onSubmit: (submission: McpSubmission) => void;
  onCancel: () => void;
  /** A draft to open on, e.g. one seeded from a catalog preset. */
  initial: McpDraft;
  isActive?: boolean;
  width: number;
}

type MenuChoice = { key: string; label: string; hint?: string };

const CONNECTED_MENU: MenuChoice[] = [
  { key: "all", label: "Enable all", hint: "register every tool the server has" },
  { key: "select", label: "Select tools", hint: "tick the ones this project needs" },
  { key: "cancel", label: "Cancel", hint: "nothing is written" },
];

const FAILED_MENU: MenuChoice[] = [
  { key: "retry", label: "Retry", hint: "probe it again" },
  { key: "save", label: "Save anyway", hint: "write the entry without a working probe" },
  { key: "cancel", label: "Cancel", hint: "nothing is written" },
];

const UNSAFE_MENU: MenuChoice[] = [
  { key: "save", label: "Save despite the warning", hint: "the finding above is accepted" },
  { key: "cancel", label: "Cancel", hint: "nothing is written" },
];

export function McpAddForm({
  onTest,
  onSubmit,
  onCancel,
  initial,
  isActive = true,
  width,
}: McpAddFormProps): React.ReactElement {
  const [draft, setDraft] = useState<McpDraft>(initial);
  const [step, setStep] = useState<McpFormStep>("name");
  const [typed, setTyped] = useState(initial.name);
  const [choice, setChoice] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<McpProbe | null>(null);

  const isUrl = draft.transport === "url";
  const varLabel = isUrl ? "header" : "env";
  const collected = isUrl ? draft.headers : draft.env;

  /** Probe the draft, then show what came back. */
  const runProbe = (next: McpDraft): void => {
    setStep("testing");
    setProbe(null);
    void onTest({ ...draftParams(next), scope: next.scope }).then((result) => {
      setProbe(result);
      setChoice(0);
      setStep(result.ok ? "connected" : "failed");
    });
  };

  /** Move to the next question, seeding the text field it owns. */
  const go = (next: McpFormStep, seed = ""): void => {
    setError(null);
    setChoice(0);
    setTyped(seed);
    setStep(next);
  };

  const menu: MenuChoice[] =
    step === "connected" ? CONNECTED_MENU : step === "failed" ? FAILED_MENU : step === "unsafe" ? UNSAFE_MENU : [];

  const pickMenu = (key: string): void => {
    if (key === "cancel") {
      onCancel();
      return;
    }
    if (key === "retry") {
      runProbe(draft);
      return;
    }
    if (key === "all") {
      onSubmit({ params: { ...draftParams(draft), test: false }, toolsInclude: null, force: false });
      return;
    }
    if (key === "select") {
      setStep("tools");
      return;
    }
    if (key === "save") {
      // A safety finding is the one refusal `force` may overrule, and only
      // after it has been read and accepted on its own screen (M14 §1b).
      if (step === "failed" && isUnsafeError(probe?.error ?? null)) {
        setChoice(1);
        setStep("unsafe");
        return;
      }
      onSubmit({
        params: { ...draftParams(draft), test: false },
        toolsInclude: null,
        force: step === "unsafe",
      });
    }
  };

  useInput(
    (input, key) => {
      if (key.escape) {
        onCancel();
        return;
      }
      if (step === "testing") return;

      if (menu.length > 0) {
        if (key.upArrow || key.downArrow || key.tab) {
          setChoice((i) => (i + (key.upArrow ? menu.length - 1 : 1)) % menu.length);
          return;
        }
        if (key.return) pickMenu(menu[choice].key);
        return;
      }

      if (step === "transport" || step === "scope") {
        const rows = step === "transport" ? TRANSPORTS : SCOPES;
        if (key.upArrow || key.downArrow || key.tab) {
          setChoice((i) => (i + (key.upArrow ? rows.length - 1 : 1)) % rows.length);
          return;
        }
        if (!key.return) return;
        if (step === "transport") {
          const transport = TRANSPORTS[choice].value;
          setDraft((current) => ({ ...current, transport }));
          go(transport === "url" ? "url" : "command", transport === "url" ? draft.url : draft.commandLine);
          return;
        }
        const scope = SCOPES[choice].value;
        const next = { ...draft, scope };
        setDraft(next);
        runProbe(next);
        return;
      }

      if (key.return) {
        const value = typed.trim();
        if (step === "name") {
          if (!isValidMcpName(value)) {
            setError("a server name is letters, digits, - or _ (max 64)");
            return;
          }
          setDraft((current) => ({ ...current, name: value }));
          setChoice(draft.transport === "url" ? 1 : 0);
          go("transport");
          return;
        }
        if (step === "command") {
          if (splitArgs(value).length === 0) {
            setError("the command to run, with its arguments: npx -y @scope/server");
            return;
          }
          setDraft((current) => ({ ...current, commandLine: value }));
          go("vars");
          return;
        }
        if (step === "url") {
          if (!/^https?:\/\/\S+$/.test(value)) {
            setError("an http or https URL");
            return;
          }
          setDraft((current) => ({ ...current, url: value }));
          go("vars");
          return;
        }
        if (step === "vars") {
          // An empty line ends the list; that is the only way out, so the
          // question is never a wall in front of a server that needs nothing.
          if (value.length === 0) {
            setChoice(draft.scope === "global" ? 1 : 0);
            go("scope");
            return;
          }
          const pair = parseAssignment(value);
          if (!pair) {
            setError(`one ${varLabel} per line, as KEY=value`);
            return;
          }
          setDraft((current) =>
            isUrl
              ? { ...current, headers: [...current.headers, pair] }
              : { ...current, env: [...current.env, pair] },
          );
          setError(null);
          setTyped("");
          return;
        }
        return;
      }

      if (key.backspace || key.delete) {
        setError(null);
        setTyped(typed.slice(0, -1));
        return;
      }
      if (key.tab || key.upArrow || key.downArrow || key.leftArrow || key.rightArrow) return;
      if (key.ctrl || key.meta || input.length === 0) return;
      setError(null);
      setTyped(typed + input);
    },
    { isActive: isActive && step !== "tools" },
  );

  if (step === "tools") {
    return (
      <ToolChecklist
        width={width}
        title={`${draft.name} — tools to register`}
        tools={probe?.tools ?? []}
        isActive={isActive}
        onCancel={() => {
          setChoice(0);
          setStep("connected");
        }}
        onSubmit={(names) =>
          onSubmit({ params: { ...draftParams(draft), test: false }, toolsInclude: names, force: false })
        }
      />
    );
  }

  const field = (label: string, value: React.ReactNode, active: boolean): React.ReactElement => (
    <Text dimColor={!active} wrap="truncate-end">
      {`  ${label.padEnd(11)}`}
      {value}
      {active ? <Text inverse>{" "}</Text> : null}
    </Text>
  );

  const done = (value: string): React.ReactElement => <Text color="green">{value}</Text>;

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        New MCP server
      </Text>

      {field("name", step === "name" ? <Text>{typed}</Text> : done(draft.name), step === "name")}

      {step === "transport" ? (
        TRANSPORTS.map((entry, index) => (
          <Text key={entry.value} inverse={index === choice} wrap="truncate-end">
            {`  ${entry.label.padEnd(11)}`}
            <Text dimColor>{entry.hint}</Text>
          </Text>
        ))
      ) : step === "name" ? null : (
        field("transport", done(isUrl ? "URL" : "Command"), false)
      )}

      {step === "command" || (!isUrl && draft.commandLine && step !== "name" && step !== "transport")
        ? field(
            "command",
            step === "command" ? <Text>{typed}</Text> : done(draft.commandLine),
            step === "command",
          )
        : null}

      {step === "url" || (isUrl && draft.url && step !== "name" && step !== "transport")
        ? field("url", step === "url" ? <Text>{typed}</Text> : done(draft.url), step === "url")
        : null}

      {collected.map((entry) => (
        <Text key={`${varLabel}-${entry.key}`} dimColor wrap="truncate-end">
          {`  ${varLabel.padEnd(11)}`}
          <Text color="green">{maskAssignment(entry.key)}</Text>
        </Text>
      ))}

      {step === "vars" ? field(varLabel, <Text>{typed}</Text>, true) : null}

      {step === "scope"
        ? SCOPES.map((entry, index) => (
            <Text key={entry.value} inverse={index === choice} wrap="truncate-end">
              {`  ${entry.label.padEnd(11)}`}
              <Text dimColor>{entry.hint}</Text>
            </Text>
          ))
        : null}

      {step === "testing" ? <Text color="yellow">{`  testing ${draft.name}…`}</Text> : null}

      {step === "connected" && probe ? (
        <Text color="green" wrap="truncate-end">{`  ${probeSummary(probe)}`}</Text>
      ) : null}

      {(step === "failed" || step === "unsafe") && probe ? (
        <Text color="red" wrap="truncate-end">{`  could not start ${draft.name}: ${probe.error ?? "no answer"}`}</Text>
      ) : null}

      {step === "unsafe" ? (
        <Text color="yellow" wrap="truncate-end">
          {"  this entry failed the safety check; saving it accepts that finding"}
        </Text>
      ) : null}

      {menu.map((entry, index) => (
        <Text key={entry.key} inverse={index === choice} wrap="truncate-end">
          {`  ${entry.label.padEnd(26)}`}
          <Text dimColor>{entry.hint ?? ""}</Text>
        </Text>
      ))}

      {error ? <Text color="red">{`  ${error}`}</Text> : null}

      <Text dimColor>
        {step === "testing"
          ? "probing the server · Esc cancel"
          : menu.length > 0 || step === "transport" || step === "scope"
            ? "↑/↓ choose · Enter pick · Esc cancel"
            : step === "vars"
              ? `Enter adds one ${varLabel} · empty Enter continues · Esc cancel`
              : "Enter next · Esc cancel"}
      </Text>
    </Box>
  );
}
