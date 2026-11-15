import { describe, expect, it } from "vitest";
import { fit } from "../src/layout/fit.js";
import { textWidth } from "../src/layout/text-width.js";
import { TuiClient } from "../src/rpc/client.js";
import { sanitizeDeep, sanitizeText } from "../src/util/sanitize.js";

// Hostile strings, as data only (security review M7): nothing here is printed.
const OSC52_BEL = "\x1b]52;c;aGVsbG8=\x07";
const OSC52_ST = "\x1b]52;c;aGVsbG8=\x1b\\";
const OSC8 = "\x1b]8;;https://example.invalid\x1b\\label\x1b]8;;\x1b\\";
const CURSOR = "\x1b[10;5H\x1b[3A\x1b[2K\x1b[1G\x1b[s\x1b[u";
const C1_OSC = "\x9d52;c;aGVsbG8=\x9c";
const C1_CSI = "\x9b2J\x9b10;5H";
const BIDI = "‮‭‪‫‬⁦⁧⁨⁩";

describe("sanitizeText: the sequences that drive a terminal", () => {
  it("removes OSC 52 (clipboard) with either terminator", () => {
    expect(sanitizeText(`a${OSC52_BEL}b`)).toBe("ab");
    expect(sanitizeText(`a${OSC52_ST}b`)).toBe("ab");
  });

  it("removes OSC 8 (hidden link) and keeps the visible label", () => {
    expect(sanitizeText(`open ${OSC8} now`)).toBe("open label now");
  });

  it("removes CSI cursor movement, erase and save/restore", () => {
    expect(sanitizeText(`x${CURSOR}y`)).toBe("xy");
  });

  it("removes the 8-bit C1 forms of OSC and CSI", () => {
    expect(sanitizeText(`a${C1_OSC}b`)).toBe("ab");
    expect(sanitizeText(`a${C1_CSI}b`)).toBe("ab");
  });

  it("removes an unterminated OSC to the end of the string", () => {
    expect(sanitizeText("ok\x1b]52;c;aGVsbG8=")).toBe("ok");
  });

  it("removes bidi override, embedding and isolate characters from a title", () => {
    expect(sanitizeText(`report${BIDI}gpj.exe`)).toBe("reportgpj.exe");
    expect(sanitizeText("שלום עולם")).toBe("שלום עולם");
  });
});

describe("a hostile title in a 60-column layout", () => {
  const hostile = `deploy ${OSC52_BEL}${OSC8}${CURSOR}${C1_CSI}${BIDI}한국어 ✅ ${"w".repeat(80)}`;

  it("measures and fits like the plain text it leaves behind", () => {
    const clean = sanitizeText(hostile);
    expect(clean).toBe(`deploy label한국어 ✅ ${"w".repeat(80)}`);
    const row = fit(clean, 60);
    expect(textWidth(row)).toBeLessThanOrEqual(60);
    expect(row).not.toMatch(/[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]/);
  });

  it("does not change text with nothing to strip", () => {
    const plain = "세션 제목 — plain title ✅";
    expect(sanitizeText(plain)).toBe(plain);
    expect(fit(sanitizeText(plain), 60)).toBe(fit(plain, 60));
  });
});

describe("TuiClient boundary covers every inbound path", () => {
  function fake() {
    const handlers = new Map<string, (params: any) => unknown>();
    const requests = new Map<string, (params: any) => unknown>();
    const results = new Map<string, unknown>();
    class Sdk {
      capabilities: string[] = [];
      on(event: string, handler: (params: any) => unknown) { handlers.set(event, handler); }
      onRequest(method: string, handler: (params: any) => unknown) { requests.set(method, handler); }
      call(method: string) {
        if (method === "settings.get") return Promise.resolve({ settings: {} });
        return Promise.resolve(results.get(method) ?? {});
      }
      sessionSeq() { return 0; }
      close() { return Promise.resolve(); }
    }
    const client = new TuiClient({ port: 1, token: "t", clientVersion: "test", connectFn: async () => new Sdk() as any });
    return { client, handlers, requests, results };
  }

  it("cleans the new route notifications, escalations, access lines and clis", async () => {
    const { client, handlers } = fake();
    const seen: Record<string, any> = {};
    client.setListeners({
      onApprovalRouteChanged: (p: any) => { seen.approval = p; },
      onQuestionRouteChanged: (p: any) => { seen.question = p; },
      onJarvisEscalation: (p: any) => { seen.escalation = p; },
      onJarvisAccess: (p: any) => { seen.access = p; },
      onExternalClisChanged: (p: any) => { seen.clis = p; },
      onApprovalPending: (p: any) => { seen.pending = p; },
      onQuestionPending: (p: any) => { seen.questionPending = p; },
    } as any);
    await client.connect();
    handlers.get("approval.route.changed")!({ requestId: "r", sessionId: `s${CURSOR}`, route: "person", reason: "paused" });
    handlers.get("question.route.changed")!({ requestId: "q", sessionId: "s", route: null, reason: "jarvis_disabled" });
    handlers.get("jarvis.escalation")!({ id: "e", summary: `done${OSC52_BEL}`, title: `t${BIDI}` });
    handlers.get("jarvis.access")!({ id: "a", action: "read", sessionId: "s", summary: `read${C1_OSC}` });
    handlers.get("external.clis.changed")!({ clis: [{ id: "claude", pendingPath: `/bin/x${OSC8}`, problems: [`bad${CURSOR}`] }] });
    handlers.get("approval.pending")!({ request: { requestId: "p", tool: `Bash${C1_CSI}`, args: { command: `ls${OSC52_ST}` } } });
    handlers.get("question.pending")!({ request: { requestId: "p", questions: [{ question: `why${BIDI}?` }] } });
    expect(seen.approval.sessionId).toBe("s");
    expect(seen.question.route).toBeNull();
    expect(seen.escalation).toMatchObject({ summary: "done", title: "t" });
    expect(seen.access.summary).toBe("read");
    expect(seen.clis.clis[0]).toMatchObject({ pendingPath: "/bin/xlabel", problems: ["bad"] });
    expect(seen.pending.request).toMatchObject({ tool: "Bash", args: { command: "ls" } });
    expect(seen.questionPending.request.questions[0].question).toBe("why?");
    await client.close();
  });

  it("cleans resumed history, replayed events and event pages", async () => {
    const { client, results } = fake();
    await client.connect();
    results.set("session.resume", {
      events: [{ seq: 1, kind: "message.assistant", payload: { text: `hi${OSC52_BEL}${CURSOR}` } }],
    });
    results.set("session.events", {
      events: [{ seq: 2, kind: "tool.call", payload: { title: `run${BIDI}${C1_CSI}` } }],
    });
    results.set("external.open", {
      session: { title: `T${OSC8}`, cwd: `/p${CURSOR}` },
      events: [{ kind: "message.user", payload: { text: `x${C1_OSC}` } }],
    });
    const resumed = await client.call("session.resume", { sessionId: "s" });
    const page = await client.call("session.events", { sessionId: "s" });
    const opened = await client.call("external.open", {});
    expect(resumed.events[0].payload.text).toBe("hi");
    expect(page.events[0].payload.title).toBe("run");
    expect(opened.session).toEqual({ title: "Tlabel", cwd: "/p" });
    expect(opened.events[0].payload.text).toBe("x");
    await client.close();
  });

  it("cleans the question request a server sends", async () => {
    const { client, requests } = fake();
    await client.connect();
    let asked: any = null;
    client.onQuestionRequest(async (req: any) => { asked = req; return { answers: [] } as any; });
    await requests.get("question.request")!({
      requestId: "q",
      questions: [{ question: `ok?${OSC52_BEL}`, options: [{ label: `a${BIDI}b` }] }],
    });
    expect(asked.questions[0]).toEqual({ question: "ok?", options: [{ label: "ab" }] });
    await client.close();
  });

  it("sanitizeDeep leaves non-strings alone", () => {
    expect(sanitizeDeep({ n: 1, b: false, a: [null, `x${BIDI}`] })).toEqual({ n: 1, b: false, a: [null, "x"] });
  });
});
