/**
 * `/settings disk`: settings.json was edited outside snowpea, and the
 * security / Jarvis / remote access sections in it wait for the person.
 *
 * `a` applies what the file says, `k` keeps the current settings (the file is
 * written back). The answer (`applied`, `discarded`) is printed on the screen;
 * any key then closes it. Lines are wrapped, so nothing is cut at 60 columns.
 */

import React, { useEffect, useRef, useState } from "react";
import { Box, useInput } from "ink";

import type { DiskResult } from "../hooks/useDiskSettings.js";
import { mouseReports } from "../input/mouse.js";
import type { Line, Segment } from "../layout/transcript.js";
import { DISK_NOTHING_WAITS, choiceLines } from "../state/disk-settings.js";
import { say } from "../state/errors.js";
import { wrapSegments } from "../state/security.js";
import { RenderedLines } from "./RenderedLines.js";

export interface DiskSettingsScreenProps {
  /** The sections that wait. */
  waiting: readonly string[];
  width: number;
  height: number;
  onDecide: (accept: boolean) => Promise<DiskResult>;
  onClose: () => void;
  isActive?: boolean;
}

type Phase = { k: "choose" } | { k: "busy" } | { k: "done"; result: DiskResult };

export function DiskSettingsScreen({ waiting, width, height, onDecide, onClose, isActive = true }: DiskSettingsScreenProps): React.ReactElement {
  const [phase, setPhase] = useState<Phase>({ k: "choose" });
  // Sections shown stay the ones asked about, even after the answer clears them.
  const [shown] = useState<readonly string[]>(waiting);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (phase.k === "busy") return;
      // Done, or settled somewhere else meanwhile: nothing left to choose.
      if (phase.k === "done" || waiting.length === 0) return onClose();
      if (key.escape || input === "q") return onClose();
      const pressed = input.toLowerCase();
      if (pressed !== "a" && pressed !== "k") return;
      setPhase({ k: "busy" });
      void onDecide(pressed === "a").then((result) => {
        if (mounted.current) setPhase({ k: "done", result });
      });
    },
    { isActive },
  );

  const inner = Math.max(20, width - 4);
  const bold = (text: string, color?: string): Segment => ({ text, bold: true, color });
  const wrap = (key: string, segments: Segment[], indent = "  "): Line[] => wrapSegments(key, segments, inner, indent);

  const head = wrap("title", [bold(say("Settings edited on disk", "디스크에서 수정된 설정"), "cyan")], "");
  const settledElsewhere = phase.k === "choose" && waiting.length === 0;
  const body: Line[] =
    phase.k === "done"
      ? wrap("result", [{ text: phase.result.ok ? phase.result.line : phase.result.message, color: phase.result.ok ? "green" : "red", bold: !phase.result.ok }])
      : settledElsewhere
        ? wrap("elsewhere", [{ text: DISK_NOTHING_WAITS(), color: "green" }])
        : choiceLines(shown).flatMap((text, index) => wrap(`c-${index}`, [{ text, color: index === 1 ? "yellow" : undefined, dimColor: index === 2 }]));
  const hint = wrap(
    "hint",
    [
      {
        text:
          phase.k === "choose" && !settledElsewhere
            ? say("a apply the file · k keep current · Esc close", "a 파일 적용 · k 현재 유지 · Esc 닫기")
            : phase.k === "busy"
              ? "…"
              : say("any key closes", "아무 키나 누르면 닫힙니다"),
        dimColor: true,
      },
    ],
    "",
  );

  return (
    <Box flexDirection="column" width={width} height={height} borderStyle="round" borderColor="yellow" paddingX={1} overflow="hidden">
      <RenderedLines lines={head} />
      <Box flexDirection="column" flexGrow={1} overflow="hidden">
        <RenderedLines lines={body} />
      </Box>
      <RenderedLines lines={hint} />
    </Box>
  );
}
