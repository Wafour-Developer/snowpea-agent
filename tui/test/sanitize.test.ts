import { describe, expect, it } from "vitest";
import { TuiClient } from "../src/rpc/client.js";
import { sanitizeDeep, sanitizeText } from "../src/util/sanitize.js";

// Escape sequences as string data only (security review M7): never printed.
const OSC52 = "\x1b]52;c;aGVsbG8=\x07";
const OSC8 = "\x1b]8;;https://example.invalid\x1b\\click\x1b]8;;\x1b\\";
const CSI = "\x1b[2J\x1b[H\x1b[31m";
const C1_CSI = "\x9b2J";

describe("sanitizeText", () => {
  it("removes OSC 52, OSC 8, CSI and C1 sequences, keeps text, newline and tab", () => {
    expect(sanitizeText(`a${OSC52}b`)).toBe("ab");
    expect(sanitizeText(`see ${OSC8} here`)).toBe("see click here");
    expect(sanitizeText(`${CSI}red`)).toBe("red");
    expect(sanitizeText(`x${C1_CSI}y`)).toBe("xy");
    expect(sanitizeText("line1\r\nline2\tcol\rover")).toBe("line1\nline2\tcolover");
    expect(sanitizeText("bell\x07 nul\x00 del\x7f")).toBe("bell nul del");
    expect(sanitizeText("plain 한국어 ✅")).toBe("plain 한국어 ✅");
  });

  it("cleans every string of a payload, keys included", () => {
    const value = { title: `t${CSI}`, rows: [{ preview: `p${OSC52}` }], n: 3, ok: true, z: null };
    expect(sanitizeDeep(value)).toEqual({ title: "t", rows: [{ preview: "p" }], n: 3, ok: true, z: null });
    expect(sanitizeDeep({ [`k${CSI}`]: "v" })).toEqual({ k: "v" });
  });
});

describe("TuiClient boundary", () => {
  it("sanitises notifications, call results and server requests before anyone sees them", async () => {
    const handlers = new Map<string, (params: any) => unknown>();
    const requests = new Map<string, (params: any) => unknown>();
    class Sdk {
      capabilities: string[] = [];
      on(event: string, handler: (params: any) => unknown) { handlers.set(event, handler); }
      onRequest(method: string, handler: (params: any) => unknown) { requests.set(method, handler); }
      call(method: string) {
        if (method === "settings.get") return Promise.resolve({ settings: {} });
        return Promise.resolve({ sessions: [{ title: `x${OSC52}y` }] });
      }
      sessionSeq() { return 0; }
      close() { return Promise.resolve(); }
    }
    const client = new TuiClient({ port: 1, token: "t", clientVersion: "test",
      connectFn: async () => new Sdk() as any });
    const seen: any[] = [];
    client.setListeners({
      onExternalState: (p: any) => seen.push(p),
      onSessionEvent: (e: any) => seen.push(e),
    } as any);
    await client.connect();
    handlers.get("external.state")!({ ref: { cli: "claude", id: "a" }, title: `T${OSC8}`, error: CSI });
    handlers.get("session.event")!({ sessionId: "s", seq: 1, kind: "message.delta",
      payload: { text: `hi${CSI}there` } });
    expect(seen[0]).toEqual({ ref: { cli: "claude", id: "a" }, title: "Tclick", error: "" });
    expect(seen[1].payload.text).toBe("hithere");
    expect(await client.call("external.list", {})).toEqual({ sessions: [{ title: "xy" }] });
    let asked: any = null;
    client.onApprovalRequest(async (req: any) => { asked = req; return { decision: "deny" } as any; });
    await requests.get("approval.request")!({ tool: `rm${CSI}`, args: { command: `ls${OSC52}` } });
    expect(asked).toEqual({ tool: "rm", args: { command: "ls" } });
    await client.close();
  });
});
