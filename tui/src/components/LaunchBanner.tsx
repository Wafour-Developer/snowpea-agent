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

import { Logo, bigLogoRows, collapsedLine } from "./Logo.js";
import { colorMode, type ColorMode } from "../layout/palette.js";
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
  /** How much colour the terminal can take; detected when not given. */
  colors?: ColorMode;
  /**
   * Rows the banner may take. Full-screen draws it inside the transcript
   * region, which a small terminal makes shorter than the banner: the tips go
   * first, then the block wordmark gives way to its one-line form.
   */
  maxRows?: number;
}

/** Which parts of the banner fit in `maxRows`. */
export function bannerParts(
  width: number,
  hasLastSession: boolean,
  maxRows = Number.POSITIVE_INFINITY,
): { bigLogo: boolean; tips: boolean } {
  const text = 2;
  const last = hasLastSession ? 3 : 0;
  const tips = TIPS.length + 2;
  const big = bigLogoRows(width) + 1;
  if (big + text + last + tips <= maxRows) return { bigLogo: true, tips: true };
  if (big + text + last <= maxRows) return { bigLogo: true, tips: false };
  return { bigLogo: false, tips: 1 + text + last + tips <= maxRows };
}

const TIPS: readonly string[] = [
  "/help lists every command the daemon offers",
  "Shift+Tab cycles plan → accept → auto",
  "/setup configures providers and models",
];

/** Centre a line in `width`, leaving it alone when it does not fit. */
export function centre(text: string, width: number): string {
  const room = Math.max(0, Math.floor(width) - [...text].length);
  return `${" ".repeat(Math.floor(room / 2))}${text}`;
}

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
  colors: paintProp,
  maxRows,
}: LaunchBannerProps): React.ReactElement {
  const parts = bannerParts(width, lastSession !== null, maxRows);
  const target = [provider, model].filter(Boolean).join("/");
  const paint = paintProp ?? colorMode(process.env, Boolean(process.stdout?.isTTY));
  const description = "Open-source multi-vendor coding agent and personal AI assistant";
  const meta = [`v${version}`, target, workdir, mode.toUpperCase()].filter(Boolean).join(" · ");
  return (
    <Box flexDirection="column" width={width}>
      {parts.bigLogo ? (
        <>
          <Logo terminalRows={terminalRows} version={version} width={width} big mode={paint} />
          <Text dimColor>{"─".repeat(Math.max(0, width))}</Text>
        </>
      ) : (
        <Text color="green" wrap="truncate-end">{collapsedLine(version)}</Text>
      )}

      <Box flexDirection="column">
        <Text wrap="truncate-end">{centre(description, width)}</Text>
        <Text dimColor wrap="truncate-end">
          {centre(meta, width)}
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
          <Text dimColor wrap="truncate-end">Press R or type /resume to continue it</Text>
        </Box>
      ) : null}

      {parts.tips ? (
      <Box marginTop={1} marginBottom={1} flexDirection="column">
        {TIPS.map((tip) => (
          <Text key={tip} dimColor wrap="truncate-end">
            {`  · ${tip}`}
          </Text>
        ))}
      </Box>
      ) : null}
    </Box>
  );
}
