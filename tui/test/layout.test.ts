/**
 * Full-screen layout: the viewport arithmetic that decides which transcript
 * lines are on screen, and the escape sequences that own the terminal.
 */

import { describe, expect, it, vi } from "vitest";

import {
  CLEAR_SCREEN,
  ENTER_ALT_SCREEN,
  EXIT_ALT_SCREEN,
  HIDE_CURSOR,
  SHOW_CURSOR,
  enterAltScreen,
  exitAltScreen,
  installAltScreen,
  type ProcessLike,
} from "../src/layout/screen.js";
import {
  HEADER_ROWS,
  STATUS_ROWS,
  approvalPromptRows,
  approvalQueueRows,
  bottomRows,
  clampScroll,
  computeLayout,
  halfPageStep,
  pageStep,
  paletteRows,
  scrollIndicator,
  sliceViewport,
} from "../src/layout/viewport.js";
import { lineText, transcriptLines, wrapLine } from "../src/layout/transcript.js";
import { initialState, reducer, type State } from "../src/state/store.js";

const lines = (count: number): string[] =>
  Array.from({ length: count }, (_, index) => `line-${index}`);

describe("sliceViewport", () => {
  it("shows the tail of the transcript when it is pinned to the bottom", () => {
    const view = sliceViewport(lines(100), 10);
    expect(view.lines).toEqual(lines(100).slice(90));
    expect(view.start).toBe(90);
    expect(view.end).toBe(100);
    expect(view.hiddenAbove).toBe(90);
    expect(view.hiddenBelow).toBe(0);
    expect(view.atBottom).toBe(true);
    expect(view.atTop).toBe(false);
  });

  it("moves the window up by the scroll offset", () => {
    const view = sliceViewport(lines(100), 10, 25);
    expect(view.start).toBe(65);
    expect(view.end).toBe(75);
    expect(view.lines[0]).toBe("line-65");
    expect(view.hiddenBelow).toBe(25);
    expect(view.atBottom).toBe(false);
  });

  it("stops at the oldest line instead of scrolling past it", () => {
    const view = sliceViewport(lines(20), 10, 999);
    expect(view.start).toBe(0);
    expect(view.scrollOffset).toBe(10);
    expect(view.atTop).toBe(true);
    expect(view.lines).toHaveLength(10);
  });

  it("returns everything when the transcript is shorter than the window", () => {
    const view = sliceViewport(lines(3), 20);
    expect(view.lines).toHaveLength(3);
    expect(view.atTop).toBe(true);
    expect(view.atBottom).toBe(true);
    expect(scrollIndicator(view)).toBeNull();
  });

  it("survives a degenerate height", () => {
    const view = sliceViewport(lines(5), 0);
    expect(view.lines).toEqual(["line-4"]);
  });

  it("reports what is hidden in either direction", () => {
    expect(scrollIndicator(sliceViewport(lines(100), 10, 25))).toBe("▲ 65 ▼ 25");
  });
});

describe("clampScroll and page steps", () => {
  it("keeps the offset inside the scrollable range", () => {
    expect(clampScroll(-5, 100, 10)).toBe(0);
    expect(clampScroll(500, 100, 10)).toBe(90);
    expect(clampScroll(12, 100, 10)).toBe(12);
    expect(clampScroll(5, 4, 10)).toBe(0);
    expect(clampScroll(Number.NaN, 100, 10)).toBe(0);
  });

  it("pages by a screen less one line, and half-pages by half", () => {
    expect(pageStep(20)).toBe(19);
    expect(pageStep(1)).toBe(1);
    expect(halfPageStep(20)).toBe(10);
    expect(halfPageStep(1)).toBe(1);
  });
});

describe("computeLayout", () => {
  it("gives the transcript every row the fixed blocks do not take", () => {
    const layout = computeLayout({ rows: 40, columns: 100, bottomRows: 1 });
    expect(layout.transcriptRows).toBe(40 - HEADER_ROWS - STATUS_ROWS - 1);
    expect(layout.headerRows + layout.transcriptRows + layout.bottomRows + layout.statusRows).toBe(
      40,
    );
  });

  it("shrinks the transcript when the input block grows", () => {
    const small = computeLayout({ rows: 40, columns: 100, bottomRows: 1 });
    const large = computeLayout({ rows: 40, columns: 100, bottomRows: 12 });
    expect(large.transcriptRows).toBe(small.transcriptRows - 11);
  });

  it("never collapses the transcript below one row", () => {
    const layout = computeLayout({ rows: 4, columns: 20, bottomRows: 11 });
    expect(layout.transcriptRows).toBe(1);
    expect(layout.columns).toBe(20);
  });
});

describe("bottomRows", () => {
  it("reserves a single row for a bare chat line", () => {
    expect(bottomRows()).toBe(1);
  });

  it("adds the slash palette, capped at its maximum height", () => {
    expect(paletteRows(0)).toBe(0);
    expect(paletteRows(3)).toBe(6);
    expect(paletteRows(50)).toBe(11);
    expect(bottomRows({ paletteCommands: 3 })).toBe(7);
  });

  it("swaps the chat line for the approval prompt when one is up", () => {
    expect(approvalPromptRows(2)).toBe(9);
    expect(bottomRows({ approvalArgs: 2 })).toBe(9);
  });

  it("counts the unattended backlog and the error row", () => {
    expect(approvalQueueRows(0)).toBe(0);
    expect(approvalQueueRows(2, false)).toBe(6);
    expect(approvalQueueRows(2, true)).toBe(8);
    expect(bottomRows({ queueRequests: 2, queueFocused: true, errorVisible: true })).toBe(10);
  });
});

describe("wrapLine", () => {
  it("leaves a short line alone", () => {
    const line = { key: "a", segments: [{ text: "hello" }] };
    expect(wrapLine(line, 20)).toEqual([line]);
  });

  it("breaks on a space and indents the continuation", () => {
    const wrapped = wrapLine({ key: "a", segments: [{ text: "alpha beta gamma delta" }] }, 12, "  ");
    expect(wrapped.length).toBeGreaterThan(1);
    expect(lineText(wrapped[0])).toBe("alpha beta");
    expect(lineText(wrapped[1]).startsWith("  ")).toBe(true);
    for (const line of wrapped) expect(lineText(line).length).toBeLessThanOrEqual(12);
  });

  it("hard-breaks a word with no spaces rather than overflowing", () => {
    const wrapped = wrapLine({ key: "a", segments: [{ text: "x".repeat(25) }] }, 10);
    expect(wrapped.length).toBe(3);
    for (const line of wrapped) expect(lineText(line).length).toBeLessThanOrEqual(10);
  });

  it("keeps each piece's styling across the break", () => {
    const wrapped = wrapLine(
      { key: "a", segments: [{ text: "aaaa ", bold: true }, { text: "bbbbbbbb", color: "red" }] },
      6,
    );
    expect(wrapped[0].segments[0].bold).toBe(true);
    expect(wrapped[1].segments[0].color).toBe("red");
  });
});

describe("transcriptLines", () => {
  /** A session with `count` assistant messages already streamed in. */
  function seeded(count: number): State {
    let state = initialState;
    for (let index = 0; index < count; index += 1) {
      state = reducer(state, { type: "user/message", text: `question ${index}` });
    }
    return state;
  }

  it("flattens the timeline into one line per rendered row, plus a gap", () => {
    const result = transcriptLines(seeded(3), 60);
    expect(result.length).toBe(6);
    expect(lineText(result[0])).toContain("question 0");
  });

  it("wraps to the given width so every line fits the viewport", () => {
    let state = initialState;
    state = reducer(state, { type: "user/message", text: "word ".repeat(40).trim() });
    const result = transcriptLines(state, 30);
    expect(result.length).toBeGreaterThan(5);
    for (const line of result) expect(lineText(line).length).toBeLessThanOrEqual(30);
  });

  it("feeds sliceViewport a window that exactly fills the rows it is given", () => {
    const layout = computeLayout({ rows: 24, columns: 80, bottomRows: 1 });
    const result = transcriptLines(seeded(60), 78);
    const view = sliceViewport(result, layout.transcriptRows);
    expect(view.lines).toHaveLength(layout.transcriptRows);
    expect(view.atBottom).toBe(true);
  });
});

describe("alternate screen buffer", () => {
  function fakeProcess(): ProcessLike & {
    handlers: Map<string, Array<(...args: unknown[]) => void>>;
    exited: number[];
  } {
    const handlers = new Map<string, Array<(...args: unknown[]) => void>>();
    const exited: number[] = [];
    return {
      handlers,
      exited,
      on(event, listener) {
        const list = handlers.get(event) ?? [];
        list.push(listener);
        handlers.set(event, list);
        return this;
      },
      removeListener(event, listener) {
        const list = (handlers.get(event) ?? []).filter((entry) => entry !== listener);
        handlers.set(event, list);
        return this;
      },
      exit(code) {
        exited.push(code ?? 0);
        return undefined;
      },
    };
  }

  it("writes the enter sequence, a clear and a cursor hide on start", () => {
    const write = vi.fn();
    enterAltScreen({ write });
    expect(write).toHaveBeenCalledWith(ENTER_ALT_SCREEN + CLEAR_SCREEN + HIDE_CURSOR);
  });

  it("writes the exit sequence and shows the cursor again", () => {
    const write = vi.fn();
    exitAltScreen({ write });
    expect(write).toHaveBeenCalledWith(SHOW_CURSOR + EXIT_ALT_SCREEN);
  });

  it("enters on install and restores on the handle", () => {
    const write = vi.fn();
    const proc = fakeProcess();
    const handle = installAltScreen({ stdout: { write }, process: proc });

    expect(write.mock.calls[0][0].startsWith(ENTER_ALT_SCREEN)).toBe(true);
    expect(handle.active).toBe(true);

    handle.restore();
    expect(write.mock.calls.at(-1)?.[0]).toContain(EXIT_ALT_SCREEN);
    expect(handle.active).toBe(false);
  });

  it("restores only once, however many exit paths fire", () => {
    const write = vi.fn();
    const proc = fakeProcess();
    const handle = installAltScreen({ stdout: { write }, process: proc });

    handle.restore();
    handle.restore();
    for (const listener of proc.handlers.get("exit") ?? []) listener();

    const exits = write.mock.calls.filter((call) => String(call[0]).includes(EXIT_ALT_SCREEN));
    expect(exits).toHaveLength(1);
  });

  it("restores on SIGTERM and exits 143", () => {
    const write = vi.fn();
    const proc = fakeProcess();
    installAltScreen({ stdout: { write }, process: proc });

    for (const listener of proc.handlers.get("SIGTERM") ?? []) listener();

    expect(write.mock.calls.at(-1)?.[0]).toContain(EXIT_ALT_SCREEN);
    expect(proc.exited).toEqual([143]);
  });

  it("restores on SIGINT and on process exit", () => {
    for (const [event, code] of [
      ["SIGINT", 130],
      ["SIGHUP", 129],
    ] as const) {
      const write = vi.fn();
      const proc = fakeProcess();
      installAltScreen({ stdout: { write }, process: proc });
      for (const listener of proc.handlers.get(event) ?? []) listener();
      expect(write.mock.calls.at(-1)?.[0]).toContain(EXIT_ALT_SCREEN);
      expect(proc.exited).toEqual([code]);
    }
  });

  it("restores before reporting an uncaught exception", () => {
    const write = vi.fn();
    const proc = fakeProcess();
    installAltScreen({ stdout: { write }, process: proc });

    const listener = (proc.handlers.get("uncaughtException") ?? [])[0] as (
      error: unknown,
    ) => void;
    listener(new Error("boom"));

    expect(write.mock.calls.some((call) => String(call[0]).includes(EXIT_ALT_SCREEN))).toBe(true);
    expect(write.mock.calls.at(-1)?.[0]).toContain("boom");
    expect(proc.exited).toEqual([1]);
  });

  it("tears the renderer down before it leaves the buffer", () => {
    // Ink's farewell frame has to be flushed while the alternate buffer is
    // still up, or it lands in the user's scrollback.
    const write = vi.fn();
    const proc = fakeProcess();
    const order: string[] = [];
    const handle = installAltScreen({ stdout: { write }, process: proc });
    handle.setBeforeRestore(() => order.push("unmount"));
    write.mockImplementation((chunk: string) => {
      if (chunk.includes(EXIT_ALT_SCREEN)) order.push("restore");
    });

    handle.restore();
    expect(order).toEqual(["unmount", "restore"]);
  });

  it("drops its process listeners once restored", () => {
    const write = vi.fn();
    const proc = fakeProcess();
    const handle = installAltScreen({ stdout: { write }, process: proc });
    handle.restore();
    const remaining = [...proc.handlers.values()].flat();
    expect(remaining).toHaveLength(0);
  });
});

describe("--fullscreen", () => {
  /** `index.tsx` self-runs on import; the guard keeps that out of the suite. */
  async function parseArgs() {
    process.env.SNOWPEA_TUI_NO_AUTORUN = "1";
    return (await import("../src/index.js")).parseArgs;
  }

  const base = ["--port", "1234", "--token", "t"];

  it("renders inline unless the alternate buffer is asked for", async () => {
    const parse = await parseArgs();
    expect(parse(base, "/tmp", {}).fullscreen).toBe(false);
  });

  it("takes the alternate buffer for the flag", async () => {
    const parse = await parseArgs();
    expect(parse([...base, "--fullscreen"], "/tmp", {}).fullscreen).toBe(true);
    expect(parse([...base, "--fullscreen=true"], "/tmp", {}).fullscreen).toBe(true);
  });

  it("keeps the inline flags working, redundant as they now are", async () => {
    const parse = await parseArgs();
    expect(parse([...base, "--fullscreen", "--no-fullscreen"], "/tmp", {}).fullscreen).toBe(false);
    expect(parse([...base, "--fullscreen", "--inline"], "/tmp", {}).fullscreen).toBe(false);
    expect(parse([...base, "--fullscreen=false"], "/tmp", {}).fullscreen).toBe(false);
    expect(parse([...base, "--fullscreen"], "/tmp", { SNOWPEA_TUI_INLINE: "1" }).fullscreen).toBe(
      false,
    );
  });

  it("does not let the standalone flag swallow the next argument", async () => {
    const parse = await parseArgs();
    const args = parse(["--fullscreen", "--port", "4321", "--token", "t"], "/tmp", {});
    expect(args.port).toBe(4321);
    expect(args.fullscreen).toBe(true);
  });
});
