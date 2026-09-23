/**
 * Deciding whether what was pasted into the input is a file.
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  MAX_ATTACHMENT_BYTES,
  __resetAttachmentIds,
  addAttachments,
  chipLabel,
  formatSize,
  looksLikePath,
  mimeFor,
  normalizePath,
  pathCandidates,
  removeLast,
  scanAttachments,
  stripPasteMarkers,
  type Attachment,
  type FileProbe,
} from "../src/state/attachments.js";

/** A disk with exactly the files the test names. */
function probeOf(files: Record<string, number>): FileProbe {
  return {
    resolve: (path) => (path.startsWith("/") ? path : `/work/${path}`),
    size: (path) => files[path] ?? null,
    basename: (path) => path.slice(path.lastIndexOf("/") + 1),
  };
}

beforeEach(() => {
  __resetAttachmentIds();
});

describe("mimeFor", () => {
  it("knows the types worth attaching", () => {
    expect(mimeFor("/a/shot.PNG")).toBe("image/png");
    expect(mimeFor("/a/spec.pdf")).toBe("application/pdf");
    expect(mimeFor("/a/notes.md")).toBe("text/markdown");
    expect(mimeFor("/a/archive.zip")).toBeNull();
    expect(mimeFor("/a/no-extension")).toBeNull();
  });
});

describe("normalizePath", () => {
  it("undoes what a terminal or a file manager added", () => {
    expect(normalizePath('"/a/my file.png"')).toBe("/a/my file.png");
    expect(normalizePath("'/a/b.png'")).toBe("/a/b.png");
    expect(normalizePath("/a/my\\ file.png")).toBe("/a/my file.png");
    expect(normalizePath("file:///a/my%20file.png")).toBe("/a/my file.png");
    expect(normalizePath("file://localhost/a/b.png")).toBe("/a/b.png");
  });

  it("strips the bracketed-paste markers", () => {
    expect(stripPasteMarkers("\u001B[200~/a/b.png\u001B[201~")).toBe("/a/b.png");
    expect(stripPasteMarkers("[200~/a/b.png[201~")).toBe("/a/b.png");
  });
});

describe("pathCandidates", () => {
  it("offers a whole line before its pieces", () => {
    const candidates = pathCandidates("/a/my file.png");
    expect(candidates[0]).toBe("/a/my file.png");
    expect(candidates).toContain("/a/my");
  });

  it("splits a multi-file drop", () => {
    const candidates = pathCandidates("/a/one.png /a/two.pdf");
    expect(candidates).toContain("/a/one.png");
    expect(candidates).toContain("/a/two.pdf");
  });

  it("takes one path per line", () => {
    expect(pathCandidates("/a/one.png\n/a/two.pdf")).toEqual(["/a/one.png", "/a/two.pdf"]);
  });
});

describe("looksLikePath", () => {
  it("recognises the shapes a path comes in", () => {
    expect(looksLikePath("/a/b.png")).toBe(true);
    expect(looksLikePath("./b.png")).toBe(true);
    expect(looksLikePath("~/b.png")).toBe(true);
    expect(looksLikePath("file:///a/b.png")).toBe(true);
    expect(looksLikePath("C:\\\\users\\\\a.png")).toBe(true);
    expect(looksLikePath("just some words")).toBe(false);
    expect(looksLikePath("b")).toBe(false);
  });
});

describe("scanAttachments", () => {
  it("turns an existing file into an attachment", () => {
    const probe = probeOf({ "/a/shot.png": 1_260_000 });
    const { attachments } = scanAttachments("/a/shot.png", probe);
    expect(attachments).toHaveLength(1);
    expect(attachments[0]).toMatchObject({
      name: "shot.png",
      path: "/a/shot.png",
      mime: "image/png",
      size: 1_260_000,
    });
    expect(chipLabel(attachments[0])).toBe("📎 shot.png 1.2MB");
  });

  it("resolves a relative path against the working directory", () => {
    const probe = probeOf({ "/work/docs/spec.pdf": 2048 });
    const { attachments } = scanAttachments("docs/spec.pdf", probe);
    expect(attachments[0]?.path).toBe("/work/docs/spec.pdf");
  });

  it("takes a file whose name has spaces, dropped whole", () => {
    const probe = probeOf({ "/a/my file.png": 10 });
    const { attachments } = scanAttachments("/a/my file.png", probe);
    expect(attachments.map((a) => a.path)).toEqual(["/a/my file.png"]);
  });

  it("takes several files from one drop", () => {
    const probe = probeOf({ "/a/one.png": 10, "/a/two.pdf": 20 });
    const { attachments } = scanAttachments("/a/one.png /a/two.pdf", probe);
    expect(attachments.map((a) => a.name).sort()).toEqual(["one.png", "two.pdf"]);
  });

  it("ignores text that names nothing on disk", () => {
    const probe = probeOf({});
    expect(scanAttachments("please look at /a/missing.png", probe).attachments).toEqual([]);
  });

  it("says why a file it found cannot be attached", () => {
    const probe = probeOf({ "/a/huge.png": MAX_ATTACHMENT_BYTES + 1, "/a/thing.zip": 10 });
    const { attachments, rejected } = scanAttachments("/a/huge.png\n/a/thing.zip", probe);
    expect(attachments).toEqual([]);
    const reasons = Object.fromEntries(rejected.map((entry) => [entry.path, entry.reason]));
    expect(reasons["/a/huge.png"]).toContain("too large");
    expect(reasons["/a/thing.zip"]).toBe("unsupported file type");
  });

  it("never attaches the same file twice", () => {
    const probe = probeOf({ "/a/one.png": 10 });
    const { attachments } = scanAttachments("/a/one.png /a/one.png", probe);
    expect(attachments).toHaveLength(1);
  });
});

describe("the chip list", () => {
  const chip = (id: string, path: string): Attachment => ({
    id,
    name: path.slice(path.lastIndexOf("/") + 1),
    path,
    mime: "image/png",
    size: 10,
  });

  it("adds, skipping what is already there", () => {
    const current = [chip("a1", "/a/one.png")];
    const next = addAttachments(current, [chip("a2", "/a/one.png"), chip("a3", "/a/two.png")]);
    expect(next.map((entry) => entry.path)).toEqual(["/a/one.png", "/a/two.png"]);
  });

  it("removes the newest first", () => {
    const current = [chip("a1", "/a/one.png"), chip("a2", "/a/two.png")];
    expect(removeLast(current).map((entry) => entry.path)).toEqual(["/a/one.png"]);
    expect(removeLast([])).toEqual([]);
  });
});

describe("formatSize", () => {
  it("rounds to something a chip can hold", () => {
    expect(formatSize(12)).toBe("12B");
    expect(formatSize(840 * 1024)).toBe("840KB");
    expect(formatSize(1_260_000)).toBe("1.2MB");
    expect(formatSize(-1)).toBe("0B");
  });
});


describe("a paste that is prose about a file", () => {
  it("is not turned into chips or a refusal, so the text reaches the input", () => {
    // A wine log: the second word resolves to a real .exe in the workdir.
    const probe = probeOf({ "/work/StarCraft.exe": 10 });
    const scan = scanAttachments(
      "wine StarCraft.exe\n0734:fixme:ntdll:NtQuerySystemInformation info_class\n",
      probe,
    );
    expect(scan.attachments).toEqual([]);
    expect(scan.rejected).toEqual([]);
    expect(scan.textLines).toBe(2);
  });

  it("still takes a line that is one path, and a drop of several", () => {
    const probe = probeOf({ "/w/a.png": 10, "/w/b.png": 10 });
    expect(scanAttachments("/w/a.png", probe).textLines).toBe(0);
    const drop = scanAttachments("/w/a.png /w/b.png", probe);
    expect(drop.attachments.map((entry) => entry.path)).toEqual(["/w/a.png", "/w/b.png"]);
    expect(drop.textLines).toBe(0);
  });

  it("counts a line that mixes a path with words as text", () => {
    const probe = probeOf({ "/w/a.png": 10 });
    const scan = scanAttachments("look at /w/a.png please", probe);
    expect(scan.attachments).toEqual([]);
    expect(scan.textLines).toBe(1);
  });
});
