import { describe, expect, it } from "vitest";

import { buildHudSegments, layoutHud } from "../src/layout/hud.js";
import { bottomRows } from "../src/layout/viewport.js";
import { TuiClient } from "../src/rpc/client.js";

describe("TuiClient and the 1.53.0 notifications", () => {
  it("hands each one to its listener, and the capabilities to the app", async () => {
    const handlers = new Map<string, (params: unknown) => void>();
    const sdk = {
      capabilities: ["jarvis", "externalSessions"],
      on(event: string, handler: (params: unknown) => void) {
        handlers.set(event, handler);
      },
      close: async () => undefined,
      call: async () => ({}),
    };
    const client = new TuiClient({ port: 1, token: "t", clientVersion: "x", connectFn: async () => sdk as never });
    const seen: Record<string, unknown> = {};
    client.setListeners({
      onJarvisState: (p) => (seen.state = p),
      onJarvisDecision: (p) => (seen.decision = p),
      onJarvisEscalation: (p) => (seen.escalation = p),
      onJarvisDelegation: (p) => (seen.delegation = p),
      onExternalEvent: (p) => (seen.event = p),
      onExternalState: (p) => (seen.externalState = p),
      onQuestionPending: (p) => (seen.question = p),
    });
    await client.connect();
    expect(client.serverCapabilities()).toEqual(["jarvis", "externalSessions"]);
    const names: Record<string, string> = {
      "jarvis.state": "state",
      "jarvis.decision": "decision",
      "jarvis.escalation": "escalation",
      "jarvis.delegation.changed": "delegation",
      "external.event": "event",
      "external.state": "externalState",
      "question.pending": "question",
    };
    for (const [event, key] of Object.entries(names)) {
      handlers.get(event)?.({ marker: key });
      expect(seen[key], event).toEqual({ marker: key });
    }
  });

  it("does not need the listeners at all", async () => {
    const handlers = new Map<string, (params: unknown) => void>();
    const sdk = { capabilities: [], on: (e: string, h: (p: unknown) => void) => handlers.set(e, h), close: async () => undefined };
    const client = new TuiClient({ port: 1, token: "t", clientVersion: "x", connectFn: async () => sdk as never });
    await client.connect();
    expect(() => handlers.get("external.event")?.({})).not.toThrow();
  });
});

describe("the HUD for Jarvis and external sessions", () => {
  const base = {
    status: "connected" as const,
    version: "0.2.88",
    provider: null,
    model: null,
    mode: "accept" as const,
    usage: { inputTokens: 0, outputTokens: 0 },
    sessionMs: 0,
    pendingApprovals: 0,
  };

  it("carries the Jarvis marker and the unrestricted warning only when there are some", () => {
    const keys = (input = {}) => buildHudSegments({ ...base, ...input }).map((s) => s.key);
    expect(keys()).not.toContain("jarvis");
    expect(keys()).not.toContain("external-warning");
    const segments = buildHudSegments({ ...base, jarvis: "Jarvis paused, returns in 7 min", externalWarning: "UNRESTRICTED — snowpea does not control what this Codex session runs" });
    expect(segments.find((s) => s.key === "jarvis")?.text).toBe("◆ Jarvis paused, returns in 7 min");
    expect(segments.find((s) => s.key === "external-warning")).toMatchObject({ color: "red", bold: true, priority: 0 });
  });

  it("drops the marker before the warning when the row is short", () => {
    const segments = buildHudSegments({ ...base, jarvis: "Jarvis deciding · full", externalWarning: "UNRESTRICTED" });
    const rows = layoutHud(segments, 40).flat().map((s) => s.key);
    expect(rows).toContain("external-warning");
    expect(rows).toContain("status");
  });
});

describe("the bottom block with a panel or picker", () => {
  it("reserves the extra rows", () => {
    expect(bottomRows({ extraRows: 9 })).toBe(bottomRows() + 9);
    expect(bottomRows({ extraRows: -3 })).toBe(bottomRows());
  });
});

describe("the unattended queue", () => {
  it("badges a request that belongs to an external CLI or that Jarvis is deciding", async () => {
    const { queueBadge } = await import("../src/components/ApprovalQueue.js");
    expect(queueBadge({})).toBe("");
    expect(queueBadge({ external: { cli: "codex", id: "x" } })).toBe("[Codex] ");
    expect(queueBadge({ route: "jarvis" })).toBe("[Jarvis deciding] ");
    expect(queueBadge({ route: "person" })).toBe("");
  });
});
