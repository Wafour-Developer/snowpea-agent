/**
 * Inline diffs: the header line, the counts, and what a creation looks like.
 */

import { describe, expect, it } from "vitest";

import { COLLAPSED_LINES, diffHeader, diffLineColor, diffStats } from "../src/components/DiffView.js";
import { isCreation } from "../src/state/store.js";

const EDIT = [
  "--- a/README.md",
  "+++ b/README.md",
  "@@ -1,4 +1,6 @@",
  " intro",
  "-old line",
  "+new line",
  "+another new line",
  "+a third",
  " tail",
].join("\n");

const CREATE = ["--- /dev/null", "+++ b/notes.md", "@@ -0,0 +1,2 @@", "+first", "+second"].join("\n");

describe("diffStats", () => {
  it("counts the body, not the headers", () => {
    expect(diffStats(EDIT)).toEqual({ added: 3, removed: 1 });
    expect(diffStats(CREATE)).toEqual({ added: 2, removed: 0 });
  });
});

describe("isCreation", () => {
  it("spots the /dev/null marker", () => {
    expect(isCreation(CREATE)).toBe(true);
    expect(isCreation(EDIT)).toBe(false);
  });

  it("treats an all-additions patch as a creation", () => {
    expect(isCreation("+one\n+two")).toBe(true);
    expect(isCreation("")).toBe(false);
  });
});

describe("diffHeader", () => {
  it("names the file and what changed in it", () => {
    expect(diffHeader({ id: "d1", path: "README.md", patch: EDIT })).toBe(
      "✎ Edited README.md  (+3 −1)",
    );
  });

  it("says a new file is new, and how big it is", () => {
    expect(diffHeader({ id: "d2", path: "notes.md", patch: CREATE, created: true })).toBe(
      "✚ Created notes.md (2 lines)",
    );
    expect(diffHeader({ id: "d3", path: "one.md", patch: "+only", created: true })).toBe(
      "✚ Created one.md (1 line)",
    );
  });
});

describe("diffLineColor", () => {
  it("colours additions, removals, hunks and headers apart", () => {
    expect(diffLineColor("+added")).toBe("green");
    expect(diffLineColor("-removed")).toBe("red");
    expect(diffLineColor("@@ -1 +1 @@")).toBe("magenta");
    expect(diffLineColor("--- a/x")).toBe("cyan");
    expect(diffLineColor(" context")).toBeUndefined();
  });
});

describe("collapsing", () => {
  it("keeps the first dozen lines of a long patch", () => {
    expect(COLLAPSED_LINES).toBe(12);
  });
});
