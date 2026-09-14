/**
 * The one choice primitive, key by key (M15b §2).
 *
 * Every picker in the TUI is now this list plus a handful of callbacks, so the
 * contract's keyboard table is checked here once, in both modes, against a
 * real Ink render and real keystrokes. The pickers' own tests then only have
 * to prove they wired the callbacks up to the right thing.
 */

import React, { useState } from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { ChoiceList } from "../src/components/ChoiceList.js";
import { choiceHint, useChoiceKeys } from "../src/hooks/useChoiceKeys.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const UP = "[A";
const DOWN = "[B";
const RIGHT = "[C";
const LEFT = "[D";
const ESC = "";
const ENTER = "\r";
const TAB = "\t";

const OPTIONS = [
  { label: "three.js (recommended)", description: "quick start", preview: "new Scene()" },
  { label: "Raw WebGL2", description: "total control" },
  { label: "Canvas 2D" },
];

interface Log {
  entered: number[];
  toggled: number[];
  cancelled: number;
  left: number;
  right: number;
  shortcut: string[];
}

function Harness({
  multi = false,
  allowOther = false,
  tabs = false,
  shortcuts,
  log,
}: {
  multi?: boolean;
  allowOther?: boolean;
  tabs?: boolean;
  shortcuts?: string[];
  log: Log;
}): React.ReactElement {
  const [index, setIndex] = useState(0);
  const [checked, setChecked] = useState<ReadonlySet<number>>(new Set());
  const rows = OPTIONS.length + (allowOther ? 1 : 0);

  const toggle = (at: number): void => {
    log.toggled.push(at);
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(at)) next.delete(at);
      else next.add(at);
      return next;
    });
  };

  useChoiceKeys({
    count: rows,
    index,
    onIndex: setIndex,
    multi,
    onToggle: toggle,
    onEnter: (at) => log.entered.push(at),
    onCancel: () => {
      log.cancelled += 1;
    },
    onLeft: tabs
      ? () => {
          log.left += 1;
        }
      : undefined,
    onRight: tabs
      ? () => {
          log.right += 1;
        }
      : undefined,
    digitLimit: OPTIONS.length,
    shortcuts: Object.fromEntries(
      (shortcuts ?? []).map((key) => [key, () => log.shortcut.push(key)]),
    ),
    vim: !shortcuts,
  });

  return (
    <ChoiceList
      options={OPTIONS}
      selectedIndex={index}
      checked={checked}
      multi={multi}
      radio={!multi}
      numbered
      allowOther={allowOther}
      otherLabel="Other…"
      hint={choiceHint({ multi, tabs })}
    />
  );
}

async function open(props: Omit<Parameters<typeof Harness>[0], "log"> = {}) {
  const log: Log = { entered: [], toggled: [], cancelled: 0, left: 0, right: 0, shortcut: [] };
  const stdin = fakeStdin();
  const stdout = fakeStdout(100, 24);
  const instance = render(<Harness {...props} log={log} />, {
    stdin,
    stdout: stdout.stream,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await sleep(80);
  return { log, stdin, stdout, instance };
}

async function press(stdin: any, ...keys: string[]): Promise<void> {
  for (const key of keys) {
    stdin.write(key);
    await sleep(45);
  }
}

describe("ChoiceList", () => {
  it("draws numbered rows, the cursor, the descriptions and the preview", async () => {
    const { stdout, instance } = await open();
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("❯");
    expect(output).toContain("1. ");
    expect(output).toContain("three.js (recommended)");
    expect(output).toContain("quick start");
    // The preview belongs to the row under the cursor, which starts at 0.
    expect(output).toContain("new Scene()");
  });

  it("starts the cursor on the recommended row, which is the first one", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, ENTER);
    instance.unmount();
    expect(log.entered).toEqual([0]);
  });

  it("appends the Other row last, and only when it is allowed", async () => {
    const plain = await open();
    expect(plain.stdout.text()).not.toContain("Other…");
    plain.instance.unmount();

    const { log, stdin, stdout, instance } = await open({ allowOther: true });
    expect(stdout.text()).toContain("Other…");
    await press(stdin, DOWN, DOWN, DOWN, ENTER);
    instance.unmount();
    expect(log.entered).toEqual([OPTIONS.length]);
  });

  it("names the live keys in the footer hint", async () => {
    const { stdout, instance } = await open({ multi: true, tabs: true });
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("↑↓ move");
    expect(output).toContain("Space toggle");
    expect(output).toContain("Enter confirm");
    expect(output).toContain("1-9 toggle");
    expect(output).toContain("←→ tabs");
    expect(output).toContain("Esc cancel");
  });
});

describe("ChoiceList keys, single-select", () => {
  it("moves with the arrows and wraps at both ends", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, DOWN, DOWN, DOWN, ENTER); // wraps back to the first row
    await press(stdin, UP, ENTER); // wraps to the last row
    instance.unmount();
    expect(log.entered).toEqual([0, OPTIONS.length - 1]);
  });

  it("moves with j and k when no letter shortcut claims them", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, "j", "j", ENTER, "k", ENTER);
    instance.unmount();
    expect(log.entered).toEqual([2, 1]);
  });

  it("jumps to a row with 1-9 and does not submit on its own", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, "3");
    expect(log.entered).toEqual([]);
    await press(stdin, ENTER);
    instance.unmount();
    expect(log.entered).toEqual([2]);
  });

  it("ignores a digit past the end of the list", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, "9", ENTER);
    instance.unmount();
    expect(log.entered).toEqual([0]);
  });

  it("swallows Space instead of treating it as a second Enter", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, " ");
    instance.unmount();
    expect(log.entered).toEqual([]);
    expect(log.toggled).toEqual([]);
  });

  it("cancels on Esc", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, ESC);
    instance.unmount();
    expect(log.cancelled).toBe(1);
  });

  it("walks tabs with ← and →, when the caller has tabs", async () => {
    const { log, stdin, instance } = await open({ tabs: true });
    await press(stdin, RIGHT, RIGHT, LEFT);
    instance.unmount();
    expect(log.right).toBe(2);
    expect(log.left).toBe(1);
  });

  it("takes letter shortcuts, and then leaves j and k alone", async () => {
    const { log, stdin, instance } = await open({ shortcuts: ["a", "n"] });
    await press(stdin, "a", "n", "j", ENTER);
    instance.unmount();
    expect(log.shortcut).toEqual(["a", "n"]);
    // vim motion is off here, so `j` moved nothing and Enter is still on row 0.
    expect(log.entered).toEqual([0]);
  });

  it("swallows every other key rather than leaking it", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, "z", "!", "Q");
    instance.unmount();
    expect(log).toMatchObject({ entered: [], toggled: [], cancelled: 0 });
  });
});

describe("ChoiceList keys, multi-select", () => {
  it("toggles the cursor row with Space and draws the box", async () => {
    const { log, stdin, stdout, instance } = await open({ multi: true });
    expect(stdout.text()).toContain("[ ]");
    await press(stdin, " ");
    expect(stdout.text()).toContain("[x]");
    await press(stdin, DOWN, " ");
    instance.unmount();
    expect(log.toggled).toEqual([0, 1]);
    expect(log.entered).toEqual([]);
  });

  it("toggles row N with a digit, and still does not submit", async () => {
    const { log, stdin, stdout, instance } = await open({ multi: true });
    await press(stdin, "2");
    expect(log.entered).toEqual([]);
    expect(stdout.text()).toContain("[x] Raw WebGL2");
    await press(stdin, ENTER);
    instance.unmount();
    expect(log.toggled).toEqual([1]);
    expect(log.entered).toEqual([1]);
  });

  it("Enter confirms the set rather than toggling the row under the cursor", async () => {
    const { log, stdin, instance } = await open({ multi: true });
    await press(stdin, " ", DOWN, ENTER);
    instance.unmount();
    expect(log.toggled).toEqual([0]);
    expect(log.entered).toEqual([1]);
  });

  it("cancels on Esc, with rows ticked", async () => {
    const { log, stdin, instance } = await open({ multi: true });
    await press(stdin, " ", ESC);
    instance.unmount();
    expect(log.cancelled).toBe(1);
  });
});

describe("ChoiceList scrolling", () => {
  it("keeps the cursor inside a window smaller than the list", async () => {
    const stdout = fakeStdout(100, 24);
    const many = Array.from({ length: 20 }, (_, index) => ({ label: `row ${index}` }));
    const instance = render(
      <ChoiceList options={many} selectedIndex={19} windowSize={5} hint={null} />,
      { stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(60);
    const output = stdout.text();
    instance.unmount();

    expect(output).toContain("row 19");
    expect(output).not.toContain("row 0 ");
  });
});

describe("Tab", () => {
  it("is swallowed by a list that gives it no meaning", async () => {
    const { log, stdin, instance } = await open();
    await press(stdin, TAB, ENTER);
    instance.unmount();
    expect(log.entered).toEqual([0]);
  });
});
