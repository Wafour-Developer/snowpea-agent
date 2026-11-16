/**
 * The `/resume` picker: the agent tag column, legend, combined list, filter and
 * opening an external row — pure rows first, then the real component at 60 columns.
 */

import React, { useState } from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { SessionPicker } from "../src/components/SessionPicker.js";
import { textWidth } from "../src/layout/text-width.js";
import type { ExternalSessionRow } from "../src/rpc/sdk.js";
import type { SessionRecord } from "../src/state/history.js";
import { applyFilter, itemText, pickerItems } from "../src/state/session-picker.js";
import { fakeStdin, fakeStdout, screen, sleep } from "./tty.js";

const HERE = "/work/snowpea";
const NOW = Date.parse("2026-11-16T12:00:00Z");
const record = (id: string, prompt: string, minutesAgo: number, patch: Partial<SessionRecord> = {}): SessionRecord => ({
  sessionId: `${id}-aaaaaaaa`,
  workdir: HERE,
  firstPrompt: prompt,
  at: NOW - minutesAgo * 60_000,
  kind: "chat",
  ...patch,
});
const external = (cli: "claude" | "codex" | "agy" | "cursor", id: string, title: string, minutesAgo: number): ExternalSessionRow =>
  ({
    ref: { cli, id },
    cwd: HERE,
    title,
    status: "idle",
    host: "none",
    updatedAt: new Date(NOW - minutesAgo * 60_000).toISOString(),
  }) as ExternalSessionRow;

const RECORDS = [
  record("s1", "fix the login bug", 5),
  record("s2", "browser research", 30, { agentFields: { hostToolsFrom: "profile-1" } }),
  record("s3", "refactor", 120),
];
const EXTERNALS = [external("claude", "c1", "write the docs", 10), external("codex", "x1", "port the parser", 60)];

describe("the picker rows", () => {
  it("lists snowpea sessions only, untagged, until external ones are included", () => {
    const view = pickerItems(RECORDS, [], HERE, false);
    expect(view.items.map((item) => item.identity.kind)).toEqual(["snowpea", "web", "snowpea"]);
    expect(view.hiddenElsewhere).toBe(0);
  });

  it("merges this folder's external sessions by recency and leaves other folders at the end", () => {
    const other = record("s9", "elsewhere", 1, { workdir: "/other/place" });
    const view = pickerItems([...RECORDS, other], EXTERNALS, HERE, true);
    expect(view.items.map((item) => item.id)).toEqual(["s1-aaaaa", "c1", "s2-aaaaa", "x1", "s3-aaaaa", "s9-aaaaa"].map((id) => id.slice(0, 8)));
    expect(view.items[view.items.length - 1].elsewhere).toBe("place");
  });

  it("filters by kind", () => {
    const view = pickerItems(RECORDS, EXTERNALS, HERE, false);
    expect(applyFilter(view.items, "codex").map((item) => item.title)).toEqual(["port the parser"]);
    expect(applyFilter(view.items, "snowpea")).toHaveLength(2);
    expect(applyFilter(view.items, "all")).toHaveLength(5);
  });

  it("cuts the title with an ellipsis and keeps the age, CJK included", () => {
    const item = pickerItems([record("s1", "한글 제목이 아주 길게 이어지는 세션의 첫 번째 프롬프트입니다 계속 계속", 5)], [], HERE, false).items[0];
    const text = itemText(item, 30, NOW);
    expect(textWidth(text)).toBeLessThanOrEqual(30);
    expect(text).toContain("…");
    expect(text.endsWith("5m ago")).toBe(true);
  });
});

function Harness(props: {
  records: SessionRecord[];
  externals: ExternalSessionRow[];
  opened: string[];
}): React.ReactElement {
  const [include, setInclude] = useState(false);
  return (
    <SessionPicker
      records={props.records}
      externals={props.externals}
      includeExternal={include}
      onToggleExternal={() => setInclude((value) => !value)}
      workdir={HERE}
      showElsewhere={false}
      onRevealElsewhere={() => undefined}
      onOpenSession={(id) => props.opened.push(`session:${id}`)}
      onOpenExternal={(row) => props.opened.push(`external:${row.ref.cli}:${row.ref.id}`)}
      onCancel={() => props.opened.push("cancel")}
      width={60}
      windowSize={8}
      now={NOW}
    />
  );
}

async function open(records: SessionRecord[], externals: ExternalSessionRow[]) {
  const stdin = fakeStdin();
  const out = fakeStdout(60, 24);
  const opened: string[] = [];
  const app = render(<Harness records={records} externals={externals} opened={opened} />, {
    stdin,
    stdout: out.stream,
    debug: false,
    patchConsole: false,
    exitOnCtrlC: false,
  });
  await sleep(150);
  const view = () => screen(out.text(), 24).join("\n");
  const press = async (key: string) => {
    stdin.write(key);
    await sleep(120);
  };
  return { view, press, opened, unmount: () => app.unmount() };
}

describe("the picker on screen at 60 columns", () => {
  it("shows no tag column and no legend while every session is snowpea", async () => {
    const { view, unmount } = await open([RECORDS[0], RECORDS[2]], EXTERNALS);
    const text = view();
    expect(text).toContain("fix the login bug");
    expect(text).not.toMatch(/\[(cc|cx|web)\]/);
    expect(text).not.toContain("Claude Code");
    expect(text).toContain("external sessions: hidden (e shows)");
    unmount();
  });

  it("tags a browser session and names it in the legend", async () => {
    const { view, unmount } = await open(RECORDS, EXTERNALS);
    const text = view();
    expect(text).toMatch(/\[web\]\s+browser research/);
    expect(text).toMatch(/^\s*❯?\s+fix the login bug/m);
    expect(text).toContain("[web] Browser");
    expect(text).not.toContain("[cc]");
    unmount();
  });

  it("adds the folder's external sessions with e and drops them again", async () => {
    const { view, press, unmount } = await open(RECORDS, EXTERNALS);
    await press("e");
    const text = view();
    expect(text).toMatch(/\[cc\]\s+write the docs/);
    expect(text).toMatch(/\[cx\]\s+port the parser/);
    expect(text).toContain("[cc] Claude Code  [cx] Codex  [web] Browser");
    expect(text).toContain("external sessions: shown (e hides)");
    await press("e");
    expect(view()).not.toContain("write the docs");
    unmount();
  });

  it("cycles the filter with f: all, snowpea, each kind present, back to all", async () => {
    const { view, press, unmount } = await open(RECORDS, EXTERNALS);
    await press("e");
    const seen: string[] = [];
    for (let i = 0; i < 6; i += 1) {
      await press("f");
      seen.push(/filter: ([a-z ]+) ·/.exec(view())?.[1] ?? "?");
    }
    expect(seen).toEqual(["snowpea", "claude code", "codex", "browser", "all", "snowpea"]);
    unmount();
  });

  it("filters the rows to the chosen kind", async () => {
    const { view, press, unmount } = await open(RECORDS, EXTERNALS);
    await press("e");
    await press("f"); // snowpea
    await press("f"); // claude code
    const text = view();
    expect(text).toContain("write the docs");
    expect(text).not.toContain("port the parser");
    expect(text).not.toContain("fix the login bug");
    unmount();
  });

  it("opens an external row through the external route, a snowpea row as a session", async () => {
    const { press, opened, unmount } = await open(RECORDS, EXTERNALS);
    await press("e");
    await press("f"); // snowpea
    await press("f"); // claude code
    await press("\r");
    expect(opened).toEqual(["external:claude:c1"]);
    await press("f"); // codex
    await press("f"); // browser
    await press("f"); // all
    await press("\r");
    expect(opened[1]).toBe("session:s1-aaaaaaaa");
    unmount();
  });

  it("keeps the tag and the title's start for a CJK title, cutting the end", async () => {
    const cjk = record("s4", "결제 모듈 리팩터링과 테스트 보강 작업을 이어서 진행합니다 매우 긴 제목", 3, {
      agentFields: { browserProvider: "host" },
    });
    const { view, unmount } = await open([cjk], []);
    const lines = view().split("\n");
    const row = lines.find((line) => line.includes("[web]")) ?? "";
    expect(row).toMatch(/\[web\]\s+결제 모듈/);
    expect(row).toContain("…");
    expect(row).toContain("3m ago");
    for (const line of lines) expect(textWidth(line)).toBeLessThanOrEqual(60);
    unmount();
  });
});
