/**
 * `/trust`: project trust, one full-screen overlay.
 *
 * Per scope (hooks, MCP servers, commands, agents, prompts, settings, auto
 * mode, external CLIs) it lists the project's control files with their state
 * and what they make snowpea do; the commands hooks run are shown with their
 * head AND tail and the full length (the end of a command is never hidden);
 * the project files they run are pinned with the project, and the commands whose
 * code cannot be pinned are named with the reason and a warning.
 *
 * Space picks a scope, `t` trusts the picked ones with exactly the digests of
 * the status on screen, `u` takes trust back, `r` reads the status again. Lines
 * come from `state/trust.ts` and are wrapped, so nothing is cut at 60 columns.
 */

import React, { useEffect, useRef, useState } from "react";
import { Box, useInput } from "ink";

import type { TrustResult } from "../hooks/useTrustScreen.js";
import { shortcutKey } from "../input/hangulKeys.js";
import { mouseReports } from "../input/mouse.js";
import { textWidth } from "../layout/text-width.js";
import type { Line, Segment } from "../layout/transcript.js";
import { say } from "../state/errors.js";
import { scrollInto, wrapSegments } from "../state/security.js";
import {
  TAMPER_LINE,
  TRUST_SCOPES,
  changedLines,
  initialSelection,
  limitLines,
  scopeTitle,
  trustBody,
  trustHeader,
  trustRows,
  type TrustScope,
  type TrustView,
} from "../state/trust.js";
import { RenderedLines } from "./RenderedLines.js";

export interface TrustScreenProps {
  status: TrustView;
  width: number;
  height: number;
  /** `project.trust.set` with the scopes and the digests of the status shown. */
  onTrust: (shown: TrustView, scopes: ReadonlySet<TrustScope>) => Promise<TrustResult>;
  /** `project.trust.revoke`; `null` means every scope. */
  onRevoke: (scopes: TrustScope[] | null) => Promise<TrustResult>;
  /** `project.trust.status` again. */
  onReload: () => Promise<TrustResult>;
  /** Counts `project.trust.changed` events that concern this project. */
  changeSeq?: number;
  /** Something outside snowpea tried to change the trust records (`reason: "tamper"`); nothing was changed. */
  tampered?: boolean;
  onClose: () => void;
  isActive?: boolean;
}

type Mode = { k: "browse" } | { k: "revoke" } | { k: "busy" };

interface Note {
  text: string;
  color?: string;
  dim?: boolean;
}

export function TrustScreen({ status: initial, width, height, onTrust, onRevoke, onReload, changeSeq = 0, tampered = false, onClose, isActive = true }: TrustScreenProps): React.ReactElement {
  const [status, setStatus] = useState<TrustView>(initial);
  const [selected, setSelected] = useState<Set<TrustScope>>(() => initialSelection(initial));
  const [cursor, setCursor] = useState(0);
  const [mode, setMode] = useState<Mode>({ k: "browse" });
  const [notes, setNotes] = useState<Note[]>([]);
  /** Lines scrolled inside a block taller than the window. */
  const [inside, setInside] = useState(0);
  const scroll = useRef(0);

  const rows = trustRows(status);
  const at = Math.min(cursor, Math.max(0, rows.length - 1));
  const row = rows[at];
  const say1 = (text: string, color?: string) => setNotes([{ text, color }]);

  const take = (next: TrustView) => {
    setStatus(next);
    setCursor((current) => Math.min(current, Math.max(0, trustRows(next).length - 1)));
  };

  const reload = (quiet = false) => {
    void onReload().then((result) => {
      if (result.ok) {
        take(result.status);
        if (!quiet) say1(say("Read the project again.", "프로젝트를 다시 읽었습니다."), "green");
      } else if (result.kind === "error") say1(say(`Could not read it: ${result.message}`, `읽을 수 없습니다: ${result.message}`), "red");
    });
  };

  // `project.trust.changed` (another surface, the CLI): read the status again.
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    if (changeSeq > 0) reloadRef.current(true);
  }, [changeSeq]);

  const trust = () => {
    if (selected.size === 0) {
      say1(say("Pick at least one scope with Space.", "Space로 범위를 하나 이상 고르세요."), "yellow");
      return;
    }
    setMode({ k: "busy" });
    setNotes([{ text: say("Trusting…", "신뢰하는 중…"), dim: true }]);
    void onTrust(status, selected).then((result) => {
      setMode({ k: "browse" });
      if (result.ok) {
        take(result.status);
        say1(say(`Trusted: ${result.status.scopes.map(scopeTitle).join(", ")}. Files are pinned as they were shown.`, `신뢰함: ${result.status.scopes.map(scopeTitle).join(", ")}. 보여 드린 그대로 파일이 고정되었습니다.`), "green");
      } else if (result.kind === "changed") {
        // Nothing was trusted. Show what moved, take the reloaded status and wait for the key again.
        if (result.status) take(result.status);
        setNotes(changedLines(result.files).map((text, index) => ({ text, color: index === 0 ? "red" : index === 1 ? "yellow" : undefined, dim: index > 1 })));
      } else {
        say1(result.message, result.kind === "refused" ? "yellow" : "red");
      }
    });
  };

  const revoke = (scopes: TrustScope[] | null) => {
    setMode({ k: "busy" });
    setNotes([{ text: say("Taking trust back…", "신뢰를 거두는 중…"), dim: true }]);
    void onRevoke(scopes).then((result) => {
      setMode({ k: "browse" });
      if (result.ok) {
        take(result.status);
        say1(say("Trust taken back. The project's own control files are off again.", "신뢰를 거뒀습니다. 프로젝트 제어 파일이 다시 꺼졌습니다."), "green");
      } else if (result.kind === "changed") say1(changedLines(result.files)[0], "red");
      else say1(result.message, result.kind === "refused" ? "yellow" : "red");
    });
  };

  useInput(
    (typed, key) => {
      if (mouseReports(typed).length > 0) return;
      if (mode.k === "busy") return;
      // Every key here is a shortcut: a Korean layout's ㅕ is the u key.
      const input = shortcutKey(typed);

      if (mode.k === "revoke") {
        const lower = input.toLowerCase();
        if (lower === "s") {
          const scopes = TRUST_SCOPES.filter((scope) => selected.has(scope));
          if (scopes.length === 0) {
            setMode({ k: "browse" });
            say1(say("No scope is picked.", "고른 범위가 없습니다."), "yellow");
            return;
          }
          revoke(scopes);
        } else if (lower === "a") revoke(null);
        else if (lower === "n" || key.escape) setMode({ k: "browse" });
        return;
      }

      if (key.escape) return onClose();
      if (key.upArrow || input === "k") {
        if (rows.length > 0) setCursor((at + rows.length - 1) % rows.length);
        setInside(0);
        return;
      }
      if (key.downArrow || input === "j") {
        if (rows.length > 0) setCursor((at + 1) % rows.length);
        setInside(0);
        return;
      }
      if (key.pageDown) return setInside((value) => value + 5);
      if (key.pageUp) return setInside((value) => Math.max(0, value - 5));
      if (input === " ") {
        if (!row || row.kind !== "scope") return;
        setSelected((current) => {
          const next = new Set(current);
          if (next.has(row.scope)) next.delete(row.scope);
          else next.add(row.scope);
          return next;
        });
        setNotes([]);
        return;
      }
      switch (input.toLowerCase()) {
        case "t":
          return trust();
        case "u":
          setNotes([]);
          setMode({ k: "revoke" });
          return;
        case "r":
          return reload();
        default:
      }
    },
    { isActive },
  );

  // --- drawing -----------------------------------------------------------------

  const inner = Math.max(20, width - 4);
  const rowsLeft = Math.max(4, height - 2);
  const bold = (text: string, color?: string): Segment => ({ text, bold: true, color });

  const picked = TRUST_SCOPES.filter((scope) => selected.has(scope)).length;
  const counter = rows.length > 0 ? `${at + 1}/${rows.length}` : "";
  const titleLeft = say("Project trust", "프로젝트 신뢰");
  const titleMid = say(`  ${picked} picked`, `  ${picked}개 선택`);
  const gap = Math.max(1, inner - textWidth(titleLeft + titleMid) - textWidth(counter));
  const head: Line[] = [
    { key: "title", segments: [bold(titleLeft, "cyan"), { text: titleMid, dimColor: true }, { text: " ".repeat(gap) + counter, dimColor: true }] },
    ...(tampered ? wrapSegments("tamper", [{ text: `! ${TAMPER_LINE()}`, color: "yellow", bold: true }], inner, "  ") : []),
    ...trustHeader(status, inner),
  ];

  const foot: Line[] = [...limitLines(status, inner)];
  notes.forEach((note, index) => {
    foot.push(...wrapSegments(`note-${index}`, [{ text: note.text, color: note.color, dimColor: note.dim, bold: note.color === "red" }], inner, "  "));
  });
  if (mode.k === "revoke") {
    foot.push(
      ...wrapSegments(
        "revoke",
        [{ text: say("Take trust back: s the picked scopes · a every scope · n cancel", "신뢰 거두기: s 고른 범위 · a 모든 범위 · n 취소"), color: "yellow", bold: true }],
        inner,
        "  ",
      ),
    );
  }
  const hints =
    mode.k === "revoke"
      ? say("s picked · a all · n / Esc cancel", "s 고른 범위 · a 전체 · n / Esc 취소")
      : mode.k === "busy"
        ? "…"
        : say(
            "↑↓ move · Space pick · t trust picked · u untrust · r reload · PgUp/PgDn scroll · Esc close",
            "↑↓ 이동 · Space 선택 · t 신뢰 · u 거두기 · r 다시 읽기 · PgUp/PgDn 스크롤 · Esc 닫기",
          );
  const hint = wrapSegments("hint", [{ text: hints, dimColor: true }], inner, "");

  const bodyHeight = Math.max(2, rowsLeft - head.length - foot.length - hint.length);
  const built = trustBody(status, selected, at, inner);
  let offset = scrollInto(scroll.current, built.start, built.end, bodyHeight, built.lines.length);
  // A block taller than the window is read with PgDn: its end (a command's tail) is never out of reach.
  if (built.end - built.start > bodyHeight) {
    offset = Math.max(0, Math.min(built.start + inside, built.end - bodyHeight));
  }
  scroll.current = offset;
  const body = built.lines.slice(offset, offset + bodyHeight);

  return (
    <Box flexDirection="column" width={width} height={height} borderStyle="round" borderColor={status.trusted ? "cyan" : "yellow"} paddingX={1} overflow="hidden">
      <RenderedLines lines={head} />
      <Box flexDirection="column" flexGrow={1} overflow="hidden">
        <RenderedLines lines={body} />
      </Box>
      <RenderedLines lines={foot} />
      <RenderedLines lines={hint} />
    </Box>
  );
}
