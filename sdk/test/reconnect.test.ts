import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { WebSocketServer, type WebSocket } from "ws";

import { connect, Client, ConnectionClosedError, type ClientLifecycleEvents } from "../src/index.js";

interface FakeServer {
  port: number;
  token: string;
  server: WebSocketServer;
  clients: Set<WebSocket>;
  requests: { method: string; params: any; id: number | string }[];
  close(): Promise<void>;
  drop(): void;
}

function startFakeServer(token: string, port = 0): Promise<FakeServer> {
  return new Promise((resolve) => {
    const server = new WebSocketServer({ port, path: "/ws" }, () => {
      const addr = server.address();
      const actualPort = typeof addr === "object" && addr ? addr.port : port;
      const clients = new Set<WebSocket>();
      const requests: { method: string; params: any; id: number | string }[] = [];

      server.on("connection", (ws) => {
        clients.add(ws);
        ws.on("close", () => clients.delete(ws));
        ws.on("message", (raw) => {
          try {
            const msg = JSON.parse(String(raw));
            if (msg.method && msg.id !== undefined) {
              requests.push({ method: msg.method, params: msg.params, id: msg.id });
              if (msg.method === "system.hello") {
                if (msg.params?.token !== token) {
                  ws.send(JSON.stringify({
                    jsonrpc: "2.0",
                    id: msg.id,
                    error: { code: -32000, message: "unauthorized" },
                  }));
                  return;
                }
                ws.send(JSON.stringify({
                  jsonrpc: "2.0",
                  id: msg.id,
                  result: {
                    protocolVersion: "1.0.0",
                    serverVersion: "0.2.4",
                    capabilities: [],
                  },
                }));
              } else if (msg.method === "session.create") {
                ws.send(JSON.stringify({
                  jsonrpc: "2.0",
                  id: msg.id,
                  result: { sessionId: "sess-test" },
                }));
              } else if (msg.method === "session.resume") {
                ws.send(JSON.stringify({
                  jsonrpc: "2.0",
                  id: msg.id,
                  result: {
                    sessionId: msg.params?.sessionId,
                    events: [
                      {
                        sessionId: msg.params?.sessionId,
                        seq: 3,
                        kind: "turn.done",
                        payload: { turnId: "t-1", reason: "interrupted", synthetic: true },
                      },
                    ],
                  },
                }));
              } else if (msg.method === "session.prompt") {
                ws.send(JSON.stringify({
                  jsonrpc: "2.0",
                  id: msg.id,
                  result: { turnId: "t-2" },
                }));
              } else {
                ws.send(JSON.stringify({
                  jsonrpc: "2.0",
                  id: msg.id,
                  result: {},
                }));
              }
            }
          } catch {
            // malformed
          }
        });
      });

      resolve({
        port: actualPort,
        token,
        server,
        clients,
        requests,
        close: () => new Promise<void>((res) => {
          for (const c of clients) c.terminate();
          server.close(() => res());
        }),
        drop: () => {
          for (const c of clients) c.close(1001, "server restart");
        },
      });
    });
  });
}

describe("Client reconnect", () => {
  let tempHome: string;
  let prevHome: string | undefined;

  beforeEach(() => {
    tempHome = mkdtempSync(path.join(tmpdir(), "snowpea-test-home-"));
    prevHome = process.env.SNOWPEA_HOME;
    process.env.SNOWPEA_HOME = tempHome;
  });

  afterEach(() => {
    if (prevHome !== undefined) process.env.SNOWPEA_HOME = prevHome;
    else delete process.env.SNOWPEA_HOME;
    try {
      rmSync(tempHome, { recursive: true, force: true });
    } catch {
      // ignore
    }
  });

  it("reconnects to new port and token, resumes session, and resolves call() issued during outage", async () => {
    // 1. Start server 1
    const server1 = await startFakeServer("token-first");
    writeFileSync(
      path.join(tempHome, "daemon.json"),
      JSON.stringify({ port: server1.port, token: server1.token, pid: 123 }),
    );

    const client = await connect({
      port: server1.port,
      token: server1.token,
      clientVersion: "0.2.4",
      reconnectInitialDelayMs: 20,
      reconnectMaxDelayMs: 100,
      reconnectWaitMs: 5000,
    });

    const eventsEmitted: { reconnecting: any[]; reconnected: any[]; disconnected: any[] } = {
      reconnecting: [],
      reconnected: [],
      disconnected: [],
    };

    client.on("reconnecting", (p) => eventsEmitted.reconnecting.push(p));
    client.on("reconnected", (p) => eventsEmitted.reconnected.push(p));
    client.on("disconnected", (p) => eventsEmitted.disconnected.push(p));

    // Create a session and stream events
    const sessionRes = await client.call("session.create", { workdir: "/tmp" } as any);
    assert.equal(sessionRes.sessionId, "sess-test");

    // Simulate events from server 1
    const ws1 = [...server1.clients][0];
    ws1.send(JSON.stringify({
      jsonrpc: "2.0",
      method: "session.event",
      params: { sessionId: "sess-test", seq: 1, kind: "turn.started", payload: { turnId: "t-1" } },
    }));
    ws1.send(JSON.stringify({
      jsonrpc: "2.0",
      method: "session.event",
      params: { sessionId: "sess-test", seq: 2, kind: "message.delta", payload: { text: "working..." } },
    }));

    // Wait briefly for seq to register in client
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(client.sessionSeq("sess-test"), 2);

    // 2. Drop the socket and wait for client to enter the disconnected gap
    const disconnected = new Promise((r) => client.once("disconnected", r));
    server1.drop();
    await server1.close();
    await disconnected;

    // 3. While socket is down, issue call("session.prompt") during the gap
    let promptSettled = false;
    const promptPromise = client.call("session.prompt", {
      sessionId: "sess-test",
      text: "hello after restart",
    } as any).then((res) => {
      promptSettled = true;
      return res;
    });

    // Verify it does NOT throw immediately
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(promptSettled, false);
    assert.equal(client.connected, false);

    // 4. Start server 2 on a new port with a new token and update daemon.json
    const server2 = await startFakeServer("token-second");
    writeFileSync(
      path.join(tempHome, "daemon.json"),
      JSON.stringify({ port: server2.port, token: server2.token, pid: 456 }),
    );

    // 5. The prompt call should now resolve!
    const result = await promptPromise;
    assert.equal(promptSettled, true);
    assert.equal(result.turnId, "t-2");

    // 6. Verify handshake and resume requests reached server2
    const helloReq = server2.requests.find((r) => r.method === "system.hello");
    assert.ok(helloReq, "system.hello reached server2");
    assert.equal(helloReq.params?.token, "token-second");

    const resumeReq = server2.requests.find((r) => r.method === "session.resume");
    assert.ok(resumeReq, "session.resume reached server2");
    assert.equal(resumeReq.params?.sessionId, "sess-test");
    assert.equal(resumeReq.params?.afterSeq, 2);

    const promptReq = server2.requests.find((r) => r.method === "session.prompt");
    assert.ok(promptReq, "session.prompt reached server2");
    assert.equal(promptReq.params?.text, "hello after restart");

    // 7. Verify lifecycle events
    assert.ok(eventsEmitted.disconnected.length >= 1, "emitted disconnected");
    assert.ok(eventsEmitted.reconnecting.length >= 1, "emitted reconnecting");
    assert.ok(eventsEmitted.reconnected.length >= 1, "emitted reconnected");
    const reconnected = eventsEmitted.reconnected[0];
    assert.deepEqual(reconnected.sessions, ["sess-test"]);
    assert.deepEqual(reconnected.resumedSessions, ["sess-test"]);
    assert.equal(typeof reconnected.attempts, "number");

    await client.close();
    await server2.close();
  });

  it("call() fails with clear error when daemon is unreachable within timeout", async () => {
    const server = await startFakeServer("token-temp");
    const client = await connect({
      port: server.port,
      token: server.token,
      clientVersion: "0.2.4",
      reconnectInitialDelayMs: 20,
      reconnectMaxDelayMs: 50,
      reconnectWaitMs: 100, // 100ms timeout
    });

    server.drop();
    await server.close();

    await new Promise((r) => setTimeout(r, 40));

    await assert.rejects(
      async () => {
        await client.call("session.prompt", { sessionId: "s-1", text: "hi" } as any);
      },
      (err: Error) => {
        assert.ok(
          err.message.includes("daemon unreachable"),
          `expected daemon unreachable error, got: ${err.message}`,
        );
        return true;
      },
    );

    await client.close();
  });
});
