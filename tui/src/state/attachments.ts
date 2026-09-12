/**
 * Turning what was pasted or dropped into the input into attachments.
 *
 * A terminal has no drag-and-drop of its own: what actually arrives is text —
 * one path, several paths, a `file://` URL, or a path with the spaces escaped
 * the way a shell would. This module is the part that decides whether that text
 * is a file the user meant to attach, and it is pure apart from one injected
 * probe, so `test/attachments.test.ts` can check every shape without a disk.
 */

/** Files past this are refused; the daemon would only reject them later. */
export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;

/** What the input will carry alongside the prompt. */
export interface Attachment {
  id: string;
  /** Basename, which is what the chip shows. */
  name: string;
  path: string;
  mime: string;
  size: number;
}

/** A path that looked like an attachment but cannot be one. */
export interface RejectedAttachment {
  path: string;
  reason: string;
}

/** The file facts this module needs; `node:fs` satisfies it. */
export interface FileProbe {
  /** Size in bytes, or null when the path is not a readable file. */
  size(path: string): number | null;
  /** Absolute form of a possibly relative path. */
  resolve(path: string): string;
  /** Last segment of a path. */
  basename(path: string): string;
}

const MIME_BY_EXTENSION: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
  svg: "image/svg+xml",
  pdf: "application/pdf",
  txt: "text/plain",
  md: "text/markdown",
  json: "application/json",
  csv: "text/csv",
  log: "text/plain",
  ts: "text/plain",
  tsx: "text/plain",
  js: "text/plain",
  py: "text/plain",
  rs: "text/plain",
  go: "text/plain",
  toml: "text/plain",
  yaml: "text/yaml",
  yml: "text/yaml",
};

/** The media type a path claims by its extension, or null for the rest. */
export function mimeFor(path: string): string | null {
  const at = path.lastIndexOf(".");
  if (at === -1) return null;
  return MIME_BY_EXTENSION[path.slice(at + 1).toLowerCase()] ?? null;
}

/** `1.2MB`, `840KB`, `12B` — chip-sized, not exact. */
export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "0B";
  if (bytes < 1024) return `${Math.round(bytes)}B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

/** Strip the markers a terminal wraps a bracketed paste in. */
export function stripPasteMarkers(text: string): string {
  return text.replace(/\u001B?\[20[01]~/g, "");
}

/** `file:///home/dev/a%20b.png` → `/home/dev/a b.png`. */
function fromFileUrl(text: string): string {
  const rest = text.slice("file://".length).replace(/^localhost/, "");
  try {
    return decodeURIComponent(rest);
  } catch {
    return rest;
  }
}

/** Drop matching quotes a file manager or shell may have added. */
function unquote(text: string): string {
  const trimmed = text.trim();
  if (trimmed.length >= 2) {
    const first = trimmed[0];
    const last = trimmed[trimmed.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return trimmed.slice(1, -1);
    }
  }
  return trimmed;
}

/** One candidate path, tidied: quotes gone, escapes undone, URL decoded. */
export function normalizePath(text: string): string {
  const bare = unquote(text).replace(/\\ /g, " ");
  return bare.startsWith("file://") ? fromFileUrl(bare) : bare;
}

/**
 * Split pasted text into path candidates.
 *
 * Each line is offered whole first — a dropped file with unescaped spaces is
 * one path, not three — and then split on unescaped whitespace, which is how a
 * multi-file drop arrives. The caller decides which candidates are real by
 * asking the file system.
 */
export function pathCandidates(text: string): string[] {
  const out: string[] = [];
  for (const line of stripPasteMarkers(text).split(/[\r\n]+/)) {
    const trimmed = line.trim();
    if (trimmed.length === 0) continue;
    out.push(normalizePath(trimmed));
    const pieces = trimmed.split(/(?<!\\)\s+/).filter((piece) => piece.length > 0);
    if (pieces.length > 1) for (const piece of pieces) out.push(normalizePath(piece));
  }
  // Longest first, so the whole line wins over its own fragments.
  return [...new Set(out)].sort((a, b) => b.length - a.length);
}

/** True when the text could be a path at all; cheap enough for every keystroke. */
export function looksLikePath(text: string): boolean {
  const trimmed = stripPasteMarkers(text).trim();
  if (trimmed.length < 2) return false;
  return (
    trimmed.startsWith("file://") ||
    trimmed.startsWith("/") ||
    trimmed.startsWith("~/") ||
    trimmed.startsWith("./") ||
    trimmed.startsWith("../") ||
    /^[A-Za-z]:[\\/]/.test(trimmed) ||
    (mimeFor(trimmed) !== null && /[\\/]/.test(trimmed))
  );
}

let counter = 0;

/** Exposed so tests get stable ids. */
export function __resetAttachmentIds(): void {
  counter = 0;
}

export interface AttachmentScan {
  attachments: Attachment[];
  rejected: RejectedAttachment[];
}

/**
 * The attachments in a piece of pasted text.
 *
 * A candidate has to exist, be of a type worth sending, and fit under the size
 * limit. Anything that exists but fails the other two is reported rather than
 * dropped silently, because a chip that never appears looks like a bug.
 */
export function scanAttachments(text: string, probe: FileProbe): AttachmentScan {
  const attachments: Attachment[] = [];
  const rejected: RejectedAttachment[] = [];
  const seen = new Set<string>();

  for (const candidate of pathCandidates(text)) {
    const path = probe.resolve(candidate);
    if (seen.has(path)) continue;
    const size = probe.size(path);
    if (size === null) continue;
    seen.add(path);
    const mime = mimeFor(path);
    if (!mime) {
      rejected.push({ path, reason: "unsupported file type" });
      continue;
    }
    if (size > MAX_ATTACHMENT_BYTES) {
      rejected.push({ path, reason: `too large (${formatSize(size)}, limit 20MB)` });
      continue;
    }
    counter += 1;
    attachments.push({
      id: `att-${counter}`,
      name: probe.basename(path),
      path,
      mime,
      size,
    });
  }

  return { attachments, rejected };
}

/** Add attachments to the draft, ignoring ones already on it. */
export function addAttachments(current: Attachment[], incoming: Attachment[]): Attachment[] {
  const paths = new Set(current.map((attachment) => attachment.path));
  return [...current, ...incoming.filter((attachment) => !paths.has(attachment.path))];
}

/** Drop the newest chip, which is what Backspace on an empty input does. */
export function removeLast(current: Attachment[]): Attachment[] {
  return current.slice(0, -1);
}

/** `📎 screenshot.png 1.2MB` */
export function chipLabel(attachment: Attachment): string {
  return `📎 ${attachment.name} ${formatSize(attachment.size)}`;
}
