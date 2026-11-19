// Minimal client smoke for scripts/compat_check.py.
//
//   node compat_smoke.mjs <sdk dist dir> <port> <token> <workdir>
//
// It drives a live daemon (the NEW core) through the SDK build of an OLD
// release: hello, session.create, a turn, an approval, a question,
// session.list, session.close, close. The provider is the scripted fake one
// that compat_check.py wrote; this file only plays the client.
//
// Exit 0 and one JSON line on success; exit 1 and the failing step otherwise.

import path from "node:path";
import { pathToFileURL } from "node:url";

const [sdkDist, portArg, token, workdir] = process.argv.slice(2);
const sdk = await import(pathToFileURL(path.join(sdkDist, "index.js")).href);

const steps = [];
const step = (name) => steps.push(name);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fail(stepName, error) {
  const message = error instanceof Error ? error.message : String(error);
  console.log(JSON.stringify({ ok: false, step: stepName, error: message, done: steps }));
  process.exit(1);
}

async function waitFor(events, predicate, what, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const hit = events.find(predicate);
    if (hit) return hit;
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${what}; saw [${events.map((e) => e.kind).join(", ")}]`);
    }
    await sleep(100);
  }
}

let client;
let current = "connect (system.hello)";
try {
  client = await sdk.connect({
    port: Number(portArg),
    token,
    clientVersion: sdk.SDK_VERSION ?? "0.0.0",
    clientKind: "tui",
    reconnect: false,
  });
  step("hello");

  current = "session.create";
  const { sessionId } = await sdk.createSession(client, { workdir, mode: "accept" });
  if (!sessionId) throw new Error("session.create returned no sessionId");
  step("session.create");

  const events = [];
  client.on("session.event", (event) => {
    if (event.sessionId === sessionId) events.push(event);
  });
  const turn = async (text) => {
    const from = events.length;
    await sdk.prompt(client, sessionId, text);
    const done = await waitFor(events, (e) => e.kind === "turn.done" && events.indexOf(e) >= from, "turn.done");
    return { done, seen: events.slice(from) };
  };

  current = "turn (fake provider)";
  {
    const { done, seen } = await turn("hello");
    if (!seen.some((e) => e.kind === "message.delta")) throw new Error("no message.delta in the turn");
    if (done.payload?.reason !== "complete") throw new Error(`turn ended with ${done.payload?.reason}`);
  }
  step("turn");

  current = "approval.request (shell)";
  let asked = 0;
  client.onRequest("approval.request", async (params) => {
    asked += 1;
    return { decision: "allow", scope: "once" };
  });
  {
    const { seen } = await turn("run ls");
    if (!asked) throw new Error("the daemon never sent approval.request");
    const result = seen.find((e) => e.kind === "tool.result");
    if (!result) throw new Error("no tool.result after the approved call");
    if (result.payload?.ok === false) throw new Error("the approved shell call came back failed");
  }
  step("approval");

  current = "question.request (ask_user)";
  let questions = 0;
  client.onRequest("question.request", async (params) => {
    questions += 1;
    const first = params.questions?.[0];
    const label = first?.options?.[0]?.label;
    return { answers: [label ? { selected: [label] } : { text: "ok" }] };
  });
  {
    const { seen } = await turn("ask me");
    if (!questions) throw new Error("the daemon never sent question.request");
    const result = seen.find((e) => e.kind === "tool.result");
    if (!result) throw new Error("no tool.result after the question");
  }
  step("question");

  current = "session.list";
  {
    const listed = await sdk.listSessions(client);
    const sessions = listed.sessions ?? listed;
    const found = Array.isArray(sessions) && sessions.some((s) => (s.sessionId ?? s.id) === sessionId);
    if (!found) throw new Error(`session.list does not contain ${sessionId}: ${JSON.stringify(listed).slice(0, 300)}`);
  }
  step("session.list");

  current = "session.close";
  await sdk.closeSession(client, sessionId);
  step("session.close");

  current = "close";
  await client.close();
  step("close");
  console.log(JSON.stringify({ ok: true, done: steps, protocolVersion: client.serverProtocolVersion ?? null }));
  process.exit(0);
} catch (error) {
  try {
    await client?.close();
  } catch {
    // the failure below is the news
  }
  fail(current, error);
}
