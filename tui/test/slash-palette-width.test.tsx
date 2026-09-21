import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { SlashCommandPalette } from "../src/components/SlashCommandPalette.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

describe("the slash palette keeps command names whole", () => {
  it("truncates a long summary, never the qualified name", async () => {
    const stdin = fakeStdin();
    // 100 columns: narrow enough that the long summary has to give way.
    const stdout = fakeStdout(100, 20);
    const instance = render(
      <SlashCommandPalette
        commands={[
          { name: "ralph", summary: "Drive a task to a reviewed finish: /ralph <task>." },
          {
            name: "oh-my-claudecode:ralph",
            summary:
              "Self-referential loop until task completion with configurable verification reviewer " +
              "[--no-deslop] [--critic=architect|critic|codex] and a great deal more text after that",
          },
        ]}
      />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(40);
    const seen = stdout.text();
    instance.unmount();
    stdin.end();
    expect(seen).toContain("/oh-my-claudecode:ralph");
    expect(seen).toContain("/ralph");
  });
});
