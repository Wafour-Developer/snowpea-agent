import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { ConfirmMenu } from "../src/components/ConfirmMenu.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

const SESSIONS = Array.from({ length: 40 }, (_, i) => ({
  label: `session-${String(i + 1).padStart(2, "0")}`,
  value: `s-${i + 1}`,
}));

describe("a long menu scrolls inside a window", () => {
  it("draws a window of rows and says how many more there are", async () => {
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 24);
    const instance = render(
      <ConfirmMenu options={SESSIONS} onChoose={() => {}} windowSize={8} />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(40);
    const seen = stdout.text();
    instance.unmount();
    stdin.end();
    expect(seen).toContain("session-01");
    expect(seen).toContain("session-08");
    expect(seen).not.toContain("session-09");
    expect(seen).toContain("↓ 32 more");
    expect(seen).toContain("PgUp/PgDn");
  });

  it("PageDown moves a page and the window follows the cursor", async () => {
    const stdin = fakeStdin();
    const stdout = fakeStdout(80, 24);
    const instance = render(
      <ConfirmMenu options={SESSIONS} onChoose={() => {}} windowSize={8} />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(40);
    stdin.write("\u001b[6~"); // PageDown
    await sleep(60);
    const seen = stdout.text();
    instance.unmount();
    stdin.end();
    expect(seen).toContain("session-11");
    expect(seen).toMatch(/↑ \d+ more/);
  });
});
