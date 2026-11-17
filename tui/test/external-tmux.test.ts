import { describe, expect, it } from "vitest";

import type { ExternalSessionRow } from "../src/rpc/sdk.js";
import { parseExternalCommand } from "../src/slash/jarvis.js";
import { externalNote, hostLabel, pickerLabel, sessionModeLabel, viewOnlyReason } from "../src/state/external.js";

/** A Claude Code session the person runs in tmux pane %12, driven through the bridge (1.56.0). */
const bridged = (patch: Partial<ExternalSessionRow> = {}): ExternalSessionRow => ({
  ref: { cli: "claude", id: "0b4f-1" },
  cwd: "/work/snowpea",
  status: "idle",
  host: "tmux",
  canSend: true,
  approvals: "person",
  mode: "default",
  nativeMode: "default",
  controlled: true,
  controllable: "tmux",
  tmux: { session: "work", pane: "%12" },
  terminal: { kind: "tmux", pane: "%12", session: "work", window: "1", modeState: "confirmed", prompt: null },
  title: "fix the build",
  ...patch,
});

describe("sessions in the person's tmux pane (1.56.0)", () => {
  it("names the pane and lets the person type", () => {
    expect(hostLabel(bridged())).toBe("tmux work %12");
    expect(viewOnlyReason(bridged())).toBeNull();
    expect(pickerLabel(bridged(), 80, false)).toContain("tmux work %12");
    expect(externalNote(bridged())).toContain("your tmux pane %12");
  });

  it("says where a prompt is answered", () => {
    const waiting = bridged({ terminal: { kind: "tmux", pane: "%12", session: "work", window: "1", modeState: "confirmed", prompt: { recognised: false, text: null } } });
    expect(externalNote(waiting)).toContain("answer it there");
    expect(externalNote(waiting)).toContain("/external snapshot");
    const asked = bridged({ terminal: { kind: "tmux", pane: "%12", session: "work", window: "1", modeState: "confirmed", prompt: { recognised: true, text: "Do you want to create note.txt?" } } });
    expect(externalNote(asked)).toContain("approval queue");
  });

  it("never claims a mode the pane did not show", () => {
    expect(sessionModeLabel(bridged(), null)).toBe("default");
    const unknown = bridged({ mode: null, nativeMode: null, terminal: { kind: "tmux", pane: "%12", session: "work", window: "1", modeState: "unknown", prompt: null } });
    expect(sessionModeLabel(unknown, null)).toBe("mode not shown");
    const unconfirmed = bridged({ mode: "readonly", nativeMode: "plan", terminal: { kind: "tmux", pane: "%12", session: "work", window: "1", modeState: "unconfirmed", prompt: null } });
    expect(sessionModeLabel(unconfirmed, null)).toBe("plan (unconfirmed)");
    expect(externalNote(unconfirmed)).toContain("not confirmed");
  });

  it("keeps a terminal that is not tmux view only", () => {
    const other = bridged({ host: "terminal", canSend: false, terminal: null, controllable: null, tmux: null });
    expect(hostLabel(other)).toBe("other terminal");
    expect(viewOnlyReason(other)).toContain("view only");
  });

  it("parses /external snapshot", () => {
    expect(parseExternalCommand("/external snapshot")).toEqual({ kind: "snapshot" });
    expect(parseExternalCommand("/external snapshot now")).toMatchObject({ kind: "invalid" });
  });
});
