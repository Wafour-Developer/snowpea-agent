import { describe, expect, it } from "vitest";

import { App, PLACEHOLDER_TEXT } from "../src/app.js";

describe("App", () => {
  it("exposes the placeholder text", () => {
    expect(PLACEHOLDER_TEXT).toBe("snowpea tui placeholder");
  });

  it("is a component", () => {
    expect(typeof App).toBe("function");
  });
});
