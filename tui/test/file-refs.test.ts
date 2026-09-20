import { describe, expect, it } from "vitest";

import {
  findAllFileRefs,
  findFileRefToken,
  applyFileCompletion,
  THEME_ACCENT,
  type FileRefToken,
} from "../src/state/fileRefs.js";
import { colorizeFileRefs } from "../src/components/MessageStream.js";
import type { Line } from "../src/layout/transcript.js";

describe("token detection table", () => {
  interface TestCase {
    name: string;
    text: string;
    cursor: number;
    expected: Partial<FileRefToken> | null;
  }

  const cases: TestCase[] = [
    {
      name: "empty prompt",
      text: "",
      cursor: 0,
      expected: null,
    },
    {
      name: "cursor at start before @",
      text: "@foo",
      cursor: 0,
      expected: null,
    },
    {
      name: "bare @ at cursor 1",
      text: "@",
      cursor: 1,
      expected: { raw: "@", start: 0, end: 1, query: "", quoted: false },
    },
    {
      name: "bare @ with space before it",
      text: "hello @",
      cursor: 7,
      expected: { raw: "@", start: 6, end: 7, query: "", quoted: false },
    },
    {
      name: "unquoted file ref at cursor inside",
      text: "@foo",
      cursor: 2,
      expected: { raw: "@foo", start: 0, end: 4, query: "foo", quoted: false },
    },
    {
      name: "unquoted file ref at cursor at end",
      text: "@foo",
      cursor: 4,
      expected: { raw: "@foo", start: 0, end: 4, query: "foo", quoted: false },
    },
    {
      name: "unquoted path with directory",
      text: "@src/",
      cursor: 5,
      expected: { raw: "@src/", start: 0, end: 5, query: "src/", quoted: false },
    },
    {
      name: "unquoted file with path",
      text: "@src/a.py",
      cursor: 9,
      expected: { raw: "@src/a.py", start: 0, end: 9, query: "src/a.py", quoted: false },
    },
    {
      name: "optional line range",
      text: "@src/a.py:10-40",
      cursor: 15,
      expected: {
        raw: "@src/a.py:10-40",
        start: 0,
        end: 15,
        query: "src/a.py",
        lineRange: "10-40",
        quoted: false,
      },
    },
    {
      name: "quoted path with spaces",
      text: '@"src/my file.py"',
      cursor: 17,
      expected: {
        raw: '@"src/my file.py"',
        start: 0,
        end: 17,
        query: "src/my file.py",
        quoted: true,
      },
    },
    {
      name: "quoted path with spaces and range",
      text: '@"src/my file.py":3-5',
      cursor: 21,
      expected: {
        raw: '@"src/my file.py":3-5',
        start: 0,
        end: 21,
        query: "src/my file.py",
        lineRange: "3-5",
        quoted: true,
      },
    },
    {
      name: "unclosed quote while typing",
      text: '@"src/my fi',
      cursor: 11,
      expected: {
        raw: '@"src/my fi',
        start: 0,
        end: 11,
        query: "src/my fi",
        quoted: true,
      },
    },
    {
      name: "trailing dot punctuation stripped when sentence ends",
      text: "look at @src/a.py.",
      cursor: 17,
      expected: {
        raw: "@src/a.py",
        start: 8,
        end: 17,
        query: "src/a.py",
        quoted: false,
      },
    },
    {
      name: "parenthesis excluded",
      text: "(@src/a.py)",
      cursor: 10,
      expected: {
        raw: "@src/a.py",
        start: 1,
        end: 10,
        query: "src/a.py",
        quoted: false,
      },
    },
    {
      name: "comma excluded",
      text: "see @src/a.py, next",
      cursor: 13,
      expected: {
        raw: "@src/a.py",
        start: 4,
        end: 13,
        query: "src/a.py",
        quoted: false,
      },
    },
    {
      name: "email address is not a ref",
      text: "mail user@host.com",
      cursor: 14,
      expected: null,
    },
    {
      name: "escaped @ is not a ref",
      text: "use \\@literal",
      cursor: 10,
      expected: null,
    },
    {
      name: "multiline draft line 2 ref",
      text: "first line\n@second/file.ts",
      cursor: 26,
      expected: {
        raw: "@second/file.ts",
        start: 11,
        end: 26,
        query: "second/file.ts",
        quoted: false,
      },
    },
    {
      name: "hidden file query with dot",
      text: "@.",
      cursor: 2,
      expected: {
        raw: "@.",
        start: 0,
        end: 2,
        query: ".",
        quoted: false,
      },
    },
    {
      name: "hidden file in directory query",
      text: "@src/.",
      cursor: 6,
      expected: {
        raw: "@src/.",
        start: 0,
        end: 6,
        query: "src/.",
        quoted: false,
      },
    },
    {
      name: "korean text and emoji before ref",
      text: "안녕 🚀 @파일.txt",
      cursor: 13,
      expected: {
        raw: "@파일.txt",
        start: 6,
        end: 13,
        query: "파일.txt",
        quoted: false,
      },
    },
  ];

  cases.forEach(({ name, text, cursor, expected }) => {
    it(name, () => {
      const result = findFileRefToken(text, cursor);
      if (expected === null) {
        expect(result).toBeNull();
      } else {
        expect(result).not.toBeNull();
        expect(result).toMatchObject(expected);
      }
    });
  });
});

describe("apply/quote/dir-continue", () => {
  it("applies a simple file completion with trailing space", () => {
    const text = "read @foo now";
    const token = findFileRefToken(text, 9)!;
    expect(token).not.toBeNull();

    const result = applyFileCompletion(text, token, {
      path: "src/app.tsx",
      kind: "file",
    });

    expect(result.text).toBe("read @src/app.tsx  now");
    expect(result.cursor).toBe(18); // "read @src/app.tsx ".length
  });

  it("quotes file when path has spaces and adds trailing space", () => {
    const text = "check @foo";
    const token = findFileRefToken(text, 10)!;
    expect(token).not.toBeNull();

    const result = applyFileCompletion(text, token, {
      path: "my long file.txt",
      kind: "file",
    });

    expect(result.text).toBe('check @"my long file.txt" ');
    expect(result.cursor).toBe(result.text.length);
  });

  it("dir-continue keeps popup open with trailing slash and no space", () => {
    const text = "look @";
    const token = findFileRefToken(text, 6)!;
    expect(token).not.toBeNull();

    const result = applyFileCompletion(text, token, {
      path: "src/",
      kind: "dir",
    });

    expect(result.text).toBe("look @src/");
    expect(result.cursor).toBe(10); // at the end, right after '/'

    // Caret is inside the new token, query is "src/" so popup stays open
    const nextToken = findFileRefToken(result.text, result.cursor);
    expect(nextToken).not.toBeNull();
    expect(nextToken?.query).toBe("src/");
    expect(nextToken?.raw).toBe("@src/");
  });

  it("dir-continue quotes when directory has spaces", () => {
    const text = "look @";
    const token = findFileRefToken(text, 6)!;
    expect(token).not.toBeNull();

    const result = applyFileCompletion(text, token, {
      path: "my folder/",
      kind: "dir",
    });

    expect(result.text).toBe('look @"my folder/"');
    expect(result.cursor).toBe(result.text.length);

    // Caret is inside the token, query is "my folder/"
    const nextToken = findFileRefToken(result.text, result.cursor);
    expect(nextToken).not.toBeNull();
    expect(nextToken?.query).toBe("my folder/");
  });

  it("completing a file inside a directory closes the popup", () => {
    const text = "look @src/";
    const token = findFileRefToken(text, 10)!;
    expect(token).not.toBeNull();

    const result = applyFileCompletion(text, token, {
      path: "src/index.ts",
      kind: "file",
    });

    expect(result.text).toBe("look @src/index.ts ");
    expect(result.cursor).toBe(result.text.length);

    // Caret is after the space, so popup is closed
    const nextToken = findFileRefToken(result.text, result.cursor);
    expect(nextToken).toBeNull();
  });

  it("preserves multi-line draft around replacement", () => {
    const text = "line 1\nread @f\nline 3";
    const token = findFileRefToken(text, 14)!;
    expect(token).not.toBeNull();

    const result = applyFileCompletion(text, token, {
      path: "foo.ts",
      kind: "file",
    });

    expect(result.text).toBe("line 1\nread @foo.ts \nline 3");
  });
});

describe("MessageStream @ref coloring", () => {
  it("colorizes @refs with theme accent and ignores emails and escapes", () => {
    const lines: Line[] = [
      {
        key: "l1",
        segments: [
          { text: "› ", color: "green", bold: true },
          { text: "check @src/a.py and @\"my file.txt\" but email user@host.com or \\@escaped." },
        ],
      },
    ];

    const colored = colorizeFileRefs(lines);
    const segments = colored[0].segments;

    // Role mark unchanged
    expect(segments[0]).toEqual({ text: "› ", color: "green", bold: true });

    // File refs colored with THEME_ACCENT
    const accentSegments = segments.filter((s) => s.color === THEME_ACCENT);
    expect(accentSegments).toHaveLength(2);
    expect(accentSegments[0].text).toBe("@src/a.py");
    expect(accentSegments[1].text).toBe('@"my file.txt"');

    // Email and escaped @ remained in plain uncolored text
    const plainText = segments
      .filter((s) => !s.color)
      .map((s) => s.text)
      .join("");
    expect(plainText).toContain("user@host.com");
    expect(plainText).toContain("\\@escaped");
  });
});
