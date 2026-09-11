import assert from "node:assert/strict";
import { SDK_VERSION } from "../src/index.js";

describe("base", () => {
  it("exposes the SDK version", () => {
    assert.equal(SDK_VERSION, "0.1.0");
  });
});
