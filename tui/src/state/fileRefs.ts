/**
 * Pure helper module for '@' file references in the prompt editor.
 *
 * Prompt grammar:
 * - `@<path>` where path is relative to session workdir, absolute, or `~/…`
 * - `@"path with spaces"` for paths containing spaces
 * - Trailing punctuation `.,;:)!?` is not part of the path
 * - `\@` or an `@` preceded by a word character (e.g. emails `user@host`) is not a reference
 * - Optional line range: `@src/a.py:10-40` or `@"path with spaces":10-40`
 */

export interface FileRefToken {
  /** Raw token text as typed (e.g. "@src/a.py", '@"path with spaces"', "@") */
  raw: string;
  /** Start index in draft (at the '@') */
  start: number;
  /** End index in draft (exclusive) */
  end: number;
  /** Extracted path query for file.complete (e.g. "src/a.py", "path with spaces", "") */
  query: string;
  /** Whether the path is quoted */
  quoted: boolean;
  /** Optional line range if present (e.g. "10-40") */
  lineRange?: string;
  /** Trailing punctuation excluded from the token, if any */
  trailingPunct?: string;
}

export interface FileCompletionEntry {
  path: string;
  kind: "file" | "dir";
  size?: number | null;
}

/** Punctuation stripped from the end of an unquoted `@` reference. */
const TRAILING_PUNCT_CHARS = new Set([".", ",", ";", ":", ")", "!", "?"]);

/**
 * Returns all '@' file reference tokens found in `text`.
 */
export function findAllFileRefs(text: string): FileRefToken[] {
  const refs: FileRefToken[] = [];
  const len = text.length;
  let i = 0;

  while (i < len) {
    if (text[i] !== "@") {
      i += 1;
      continue;
    }

    // Must not be preceded by '\' or a word character (\w)
    if (i > 0) {
      const prev = text[i - 1];
      if (prev === "\\" || /\w/.test(prev)) {
        i += 1;
        continue;
      }
    }

    const start = i;

    // Check for quoted token: @"..."
    if (i + 1 < len && text[i + 1] === '"') {
      let closeIdx = -1;
      let j = i + 2;
      while (j < len && text[j] !== "\n") {
        if (text[j] === '"') {
          closeIdx = j;
          break;
        }
        j += 1;
      }

      if (closeIdx !== -1) {
        // Closed quote
        const query = text.slice(i + 2, closeIdx);
        let tokenEnd = closeIdx + 1;
        let lineRange: string | undefined;

        // Check for optional line range after quote: :10-40
        const rangeMatch = /^:(\d+(?:-\d+)?)/.exec(text.slice(tokenEnd));
        if (rangeMatch) {
          lineRange = rangeMatch[1];
          tokenEnd += rangeMatch[0].length;
        }

        refs.push({
          raw: text.slice(start, tokenEnd),
          start,
          end: tokenEnd,
          query,
          quoted: true,
          lineRange,
        });
        i = tokenEnd;
        continue;
      } else {
        // Unclosed quote up to end of line or string
        const end = j;
        const query = text.slice(i + 2, end);
        refs.push({
          raw: text.slice(start, end),
          start,
          end,
          query,
          quoted: true,
        });
        i = end;
        continue;
      }
    }

    // Unquoted token: continues until whitespace or newline or '@'
    let j = i + 1;
    while (j < len && !/\s/.test(text[j]) && text[j] !== "@") {
      j += 1;
    }

    const rawSegment = text.slice(i + 1, j);

    // Strip trailing punctuation if appropriate
    let pathEnd = j;
    let trailingPunct = "";

    // If segment is empty (just bare '@'), keep it
    if (rawSegment.length === 0) {
      refs.push({
        raw: "@",
        start,
        end: j,
        query: "",
        quoted: false,
      });
      i = j;
      continue;
    }

    // Strip trailing punctuation (.,;:)!?) from unquoted token
    // But do not strip '.' if segment is '.' or ends with '/.' (hidden file queries)
    let p = j - 1;
    while (p > i && TRAILING_PUNCT_CHARS.has(text[p])) {
      // Don't strip '.' if it's the only character after '@' or follows '/'
      if (text[p] === "." && (p === i + 1 || text[p - 1] === "/")) {
        break;
      }
      p -= 1;
    }
    trailingPunct = text.slice(p + 1, j);
    pathEnd = p + 1;

    const fullPathSegment = text.slice(i + 1, pathEnd);
    let query = fullPathSegment;
    let lineRange: string | undefined;

    // Check optional line range: :10-40 or :10
    const rangeMatch = /^(.+?):(\d+(?:-\d+)?)$/.exec(fullPathSegment);
    if (rangeMatch) {
      query = rangeMatch[1];
      lineRange = rangeMatch[2];
    }

    refs.push({
      raw: text.slice(start, pathEnd),
      start,
      end: pathEnd,
      query,
      quoted: false,
      lineRange,
      trailingPunct: trailingPunct.length > 0 ? trailingPunct : undefined,
    });

    i = j;
  }

  return refs;
}

/**
 * Finds the `@` token under the caret, or null if the caret is not inside one.
 * Caret is inside when `token.start < cursor && cursor <= token.end`.
 */
export function findFileRefToken(text: string, cursor: number): FileRefToken | null {
  if (text.length === 0 || cursor <= 0) return null;

  // If text has an open/typing unquoted token right at the caret:
  // We can locate all tokens in text and see if caret is inside any of them.
  // Special care for trailing dot actively being typed:
  // If cursor is at the end of `foo.` without following whitespace, keep dot in query.
  const len = text.length;

  // Scan backwards from cursor to find a potential '@'
  let atPos = -1;
  let inQuotes = false;

  for (let k = cursor - 1; k >= 0; k -= 1) {
    const ch = text[k];
    if (ch === "\n") break;
    if (ch === '"') inQuotes = true;
    if (ch === "@") {
      // Check if valid @
      if (k === 0 || (text[k - 1] !== "\\" && !/\w/.test(text[k - 1]))) {
        atPos = k;
      }
      break;
    }
  }

  if (atPos === -1) return null;

  // Check if quoted
  if (atPos + 1 < len && text[atPos + 1] === '"') {
    // Quoted reference
    let closeIdx = -1;
    let j = atPos + 2;
    while (j < len && text[j] !== "\n") {
      if (text[j] === '"') {
        closeIdx = j;
        break;
      }
      j += 1;
    }

    if (closeIdx !== -1) {
      let tokenEnd = closeIdx + 1;
      let lineRange: string | undefined;
      const rangeMatch = /^:(\d+(?:-\d+)?)/.exec(text.slice(tokenEnd));
      if (rangeMatch) {
        lineRange = rangeMatch[1];
        tokenEnd += rangeMatch[0].length;
      }

      if (cursor > atPos && cursor <= tokenEnd) {
        return {
          raw: text.slice(atPos, tokenEnd),
          start: atPos,
          end: tokenEnd,
          query: text.slice(atPos + 2, closeIdx),
          quoted: true,
          lineRange,
        };
      }
      return null;
    } else {
      // Unclosed quote
      const end = j;
      if (cursor > atPos && cursor <= end) {
        return {
          raw: text.slice(atPos, end),
          start: atPos,
          end,
          query: text.slice(atPos + 2, end),
          quoted: true,
        };
      }
      return null;
    }
  }

  // Unquoted token
  // Check if cursor is right after whitespace (e.g. "@foo ")
  let j = atPos + 1;
  while (j < len && !/\s/.test(text[j]) && text[j] !== "@") {
    j += 1;
  }

  // Caret must be within [atPos + 1, j]
  if (cursor <= atPos || cursor > j) return null;

  const rawSegment = text.slice(atPos + 1, j);
  if (rawSegment.length === 0) {
    // Bare '@'
    return {
      raw: "@",
      start: atPos,
      end: atPos + 1,
      query: "",
      quoted: false,
    };
  }

  // Handle trailing punctuation:
  // If cursor is at the very end of j and text[j-1] is '.':
  // If user typed `@.` or `@src/.` or `@file.`, and cursor is right after `.`,
  // we keep the `.` in query so hidden file completion and file extensions work.
  let pathEnd = j;
  let trailingPunct = "";

  let p = j - 1;
  while (p > atPos && TRAILING_PUNCT_CHARS.has(text[p])) {
    // Don't strip '.' if cursor is at this dot or if it's hidden file query
    if (text[p] === "." && (cursor === j || p === atPos + 1 || text[p - 1] === "/")) {
      break;
    }
    // Don't strip if cursor is inside this trailing punctuation
    if (cursor > p) {
      break;
    }
    p -= 1;
  }

  trailingPunct = text.slice(p + 1, j);
  pathEnd = p + 1;

  if (cursor > pathEnd && trailingPunct.length > 0) {
    // Caret is sitting in the trailing punctuation, not in the path itself
    return null;
  }

  const fullPathSegment = text.slice(atPos + 1, pathEnd);
  let query = fullPathSegment;
  let lineRange: string | undefined;

  const rangeMatch = /^(.+?):(\d+(?:-\d+)?)$/.exec(fullPathSegment);
  if (rangeMatch) {
    query = rangeMatch[1];
    lineRange = rangeMatch[2];
  }

  return {
    raw: text.slice(atPos, pathEnd),
    start: atPos,
    end: pathEnd,
    query,
    quoted: false,
    lineRange,
    trailingPunct: trailingPunct.length > 0 ? trailingPunct : undefined,
  };
}

/**
 * Applies a chosen entry to replace the `@` token.
 * - Replace the token
 * - Quote when the path has spaces: `@"path with spaces"`
 * - A directory keeps the popup open with trailing "/"
 * - A file adds a trailing space
 */
export function applyFileCompletion(
  text: string,
  token: FileRefToken,
  entry: FileCompletionEntry | string,
): { text: string; cursor: number } {
  const path = typeof entry === "string" ? entry : entry.path;
  const isDir =
    (typeof entry === "object" && entry.kind === "dir") || path.endsWith("/");
  const normalizedPath = isDir && !path.endsWith("/") ? `${path}/` : path;
  const hasSpaces = normalizedPath.includes(" ");

  let replacement: string;
  if (isDir) {
    // Directory keeps the trailing "/" and does not add a space so popup stays open
    replacement = hasSpaces ? `@"${normalizedPath}"` : `@${normalizedPath}`;
  } else {
    // File adds a trailing space to close popup and allow typing next text
    replacement = hasSpaces ? `@"${normalizedPath}" ` : `@${normalizedPath} `;
  }

  const newText = text.slice(0, token.start) + replacement + text.slice(token.end);
  const nextCursor = token.start + replacement.length;

  return { text: newText, cursor: nextCursor };
}
