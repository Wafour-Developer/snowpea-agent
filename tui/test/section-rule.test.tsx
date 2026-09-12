import React from "react";
import { render } from "ink";
import { describe, expect, it } from "vitest";

import { SectionRule } from "../src/components/SectionRule.js";
import { fakeStdin, fakeStdout } from "./tty.js";

function draw(width: number, label?: string): string {
  const stdout = fakeStdout(width, 10);
  const view = render(<SectionRule width={width} label={label} />, {
    stdin: fakeStdin(),
    stdout: stdout.stream,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  const output = stdout.text();
  view.unmount();
  return output;
}

describe("SectionRule", () => {
  it("fills exactly the requested terminal content width", () => {
    expect(draw(24)).toContain("─".repeat(24));
  });

  it("remains visible at the narrowest width", () => {
    expect(draw(1)).toContain("─");
  });

  it("places a label at the right edge without exceeding the requested width", () => {
    const output = draw(24, "Team: default");
    expect(output).toContain(`${"─".repeat(10)} Team: default`);
  });
});
