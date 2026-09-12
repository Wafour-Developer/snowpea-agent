/**
 * The first thing the session prints: what this is, where it is, and what you
 * were doing last time.
 *
 * It goes into `<Static>` as one item, so it is written once and then scrolls
 * away like any other output — a banner that redrew itself on every keystroke
 * would be the flicker all over again.
 */

import React from "react";
import { Box, Text } from "ink";

import { Logo } from "./Logo.js";
import { relativeTime, type SessionRecord } from "../state/history.js";

/** How much of the remembered prompt the launch screen shows. */
export const PROMPT_PREVIEW = 60;

export interface LaunchBannerProps {
  width: number;
  terminalRows: number;
  version: string;
  workdir: string;
  mode: string;
  provider: string | null;
  model: string | null;
  /** The session last open in this directory, when there is one. */
  lastSession?: SessionRecord | null;
  now?: number;
}

const TIPS: readonly string[] = [
  "/help lists every command the daemon offers",
  "Shift+Tab cycles plan → accept → auto",
  "/setup configures providers and models",
];

/** `"add the missing tests to the run…"` */
export function previewPrompt(text: string, max = PROMPT_PREVIEW): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 1)}…`;
}

export function LaunchBanner({
  width,
  terminalRows,
  version,
  workdir,
  mode,
  provider,
  model,
  lastSession = null,
  now = Date.now(),
}: LaunchBannerProps): React.ReactElement {
  const target = [provider, model].filter(Boolean).join("/");
  return (
    <Box flexDirection="column" width={width}>
      <Logo terminalRows={terminalRows} version={version} width={width} big />

      <Box marginTop={1} flexDirection="column">
        <Text dimColor wrap="truncate-end">
          {`Open-source multi-vendor coding agent and personal AI assistant · v${version}${
            target ? ` · ${target}` : ""
          }`}
        </Text>
        <Text dimColor wrap="truncate-end">
          {`${workdir} · ${mode.toUpperCase()} mode`}
        </Text>
      </Box>

      {lastSession ? (
        <Box marginTop={1} flexDirection="column">
          <Text wrap="truncate-end">
            <Text color="cyan">Last session: </Text>
            <Text dimColor>{`${relativeTime(lastSession.at, now)} · `}</Text>
            <Text>
              {lastSession.firstPrompt
                ? `"${previewPrompt(lastSession.firstPrompt)}"`
                : lastSession.sessionId.slice(0, 8)}
            </Text>
          </Text>
          <Text dimColor>Press R or type /resume to continue it</Text>
        </Box>
      ) : null}

      <Box marginTop={1} marginBottom={1} flexDirection="column">
        {TIPS.map((tip) => (
          <Text key={tip} dimColor wrap="truncate-end">
            {`  · ${tip}`}
          </Text>
        ))}
      </Box>
    </Box>
  );
}
