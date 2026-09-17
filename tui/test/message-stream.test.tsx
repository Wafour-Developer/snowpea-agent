import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { MessageView } from "../src/components/MessageStream.js";
import type { Message } from "../src/state/store.js";
import { fakeStdin, fakeStdout, sleep } from "./tty.js";

function assistant(text: string, streaming: boolean): Message {
  return { id: "m1", role: "assistant", text, streaming };
}

describe("MessageView", () => {
  const long = Array.from({ length: 50 }, (_, index) => `line ${index + 1}`).join("\n");

  it("shows the full text when nothing is skipped", async () => {
    const stdin = fakeStdin();
    const stdout = fakeStdout(40, 80);
    const instance = render(<MessageView message={assistant(long, false)} width={40} />, {
      stdin,
      stdout: stdout.stream,
      exitOnCtrlC: false,
      patchConsole: false,
    });
    await sleep(40);
    const seen = stdout.text();
    instance.unmount();
    stdin.end();
    expect(seen).toContain("line 1");
    expect(seen).not.toContain("lines above");
  });

  it("shows only the live tail when earlier rows are already in scrollback", async () => {
    const stdin = fakeStdin();
    const stdout = fakeStdout(40, 80);
    const instance = render(
      <MessageView message={assistant(long, true)} width={40} startLine={40} maxRows={8} />,
      { stdin, stdout: stdout.stream, exitOnCtrlC: false, patchConsole: false },
    );
    await sleep(40);
    const seen = stdout.text();
    instance.unmount();
    stdin.end();
    expect(seen).not.toContain("lines above");
    expect(seen).not.toContain("line 1");
    expect(seen).toContain("line 50");
  });
});
