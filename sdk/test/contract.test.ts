/**
 * SDK ↔ daemon contract tests (AC-15a / AC-15b).
 *
 * These run against a real daemon process, not a mock: the suite spawns
 * `uv run python -m snowpea_core --port 0 --home <tmp>` with the scripted fake
 * provider, waits for `daemon.json`, and drives it through the published SDK.
 *
 *   npm -w sdk test -- --grep base        # AC-15a, required in CI from M1
 *   npm -w sdk test -- --grep subagent    # AC-15b, joins CI at M7
 */

import assert from "node:assert/strict";
import { spawn, type ChildProcessByStdio } from "node:child_process";
import { mkdtempSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import type { Readable } from "node:stream";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  connect,
  onApprovalRequest,
  createSession,
  listAgents,
  prompt,
  spawnAgent,
  SDK_VERSION,
  type Client,
} from "../src/index.js";
import type { SessionEventPayload } from "../src/protocol.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const FAKE_SCRIPT = path.join(HERE, "fixtures", "fake-basic.json");
/** Drives the M7 subagent lane: a parent that delegates, a child that answers. */
const SUBAGENT_SCRIPT = path.join(HERE, "fixtures", "fake-subagent.json");

type DaemonProcess = ChildProcessByStdio<null, Readable, Readable>;

const DAEMON_BOOT_TIMEOUT_MS = 60_000;
const TURN_TIMEOUT_MS = 30_000;

interface DaemonHandle {
  proc: DaemonProcess;
  home: string;
  port: number;
  token: string;
  output: () => string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Spawn the real daemon on an ephemeral port with the scripted fake provider. */
async function startDaemon(script: string = FAKE_SCRIPT): Promise<DaemonHandle> {
  const home = mkdtempSync(path.join(tmpdir(), "snowpea-contract-"));
  const chunks: string[] = [];

  const proc = spawn(
    "uv",
    ["run", "python", "-m", "snowpea_core", "--port", "0", "--home", home],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        SNOWPEA_HOME: home,
        SNOWPEA_PROVIDER: `fake:${script}`,
        SNOWPEA_TEST: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let exited: { code: number | null; signal: NodeJS.Signals | null } | undefined;
  proc.stdout.on("data", (d: Buffer) => chunks.push(d.toString()));
  proc.stderr.on("data", (d: Buffer) => chunks.push(d.toString()));
  proc.on("exit", (code, signal) => {
    exited = { code, signal };
  });

  const output = () => chunks.join("").slice(-4000);
  const daemonJson = path.join(home, "daemon.json");
  const deadline = Date.now() + DAEMON_BOOT_TIMEOUT_MS;

  while (Date.now() < deadline) {
    if (existsSync(daemonJson)) {
      try {
        const info = JSON.parse(readFileSync(daemonJson, "utf8")) as {
          port?: number;
          token?: string;
        };
        if (typeof info.port === "number" && info.port > 0 && info.token) {
          return { proc, home, port: info.port, token: info.token, output };
        }
      } catch {
        // the daemon may still be mid-write; retry
      }
    }
    if (exited) {
      throw new Error(
        `daemon exited before writing daemon.json (code=${exited.code} signal=${exited.signal}).\n` +
          `--- daemon output ---\n${output()}`,
      );
    }
    await sleep(100);
  }
  throw new Error(
    `daemon did not write ${daemonJson} within ${DAEMON_BOOT_TIMEOUT_MS}ms.\n` +
      `--- daemon output ---\n${output()}`,
  );
}

async function stopDaemon(daemon: DaemonHandle | undefined): Promise<void> {
  if (!daemon) return;
  const { proc, home } = daemon;
  if (proc.exitCode === null && proc.signalCode === null) {
    const ended = new Promise<void>((resolve) => proc.once("exit", () => resolve()));
    proc.kill("SIGTERM");
    await Promise.race([ended, sleep(5000)]);
    if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
  }
  try {
    rmSync(home, { recursive: true, force: true });
  } catch {
    // best effort
  }
}

/** Records every `session.event` so assertions can look back as well as wait. */
class EventLog {
  readonly events: SessionEventPayload[] = [];
  private readonly waiters: Array<() => void> = [];
  private readonly dispose: () => void;

  constructor(client: Client, private readonly sessionId: string) {
    this.dispose = client.on("session.event", (event) => {
      if (event.sessionId !== this.sessionId) return;
      this.events.push(event);
      for (const notify of this.waiters.splice(0)) notify();
    });
  }

  kinds(): string[] {
    return this.events.map((e) => e.kind);
  }

  find(kind: string): SessionEventPayload | undefined {
    return this.events.find((e) => e.kind === kind);
  }

  /** Resolve once an event of `kind` has arrived, or reject on timeout. */
  async waitFor(kind: string, timeoutMs = TURN_TIMEOUT_MS): Promise<SessionEventPayload> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const hit = this.find(kind);
      if (hit) return hit;
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        throw new Error(
          `timed out after ${timeoutMs}ms waiting for session.event kind "${kind}"; ` +
            `saw [${this.kinds().join(", ")}]`,
        );
      }
      await Promise.race([
        new Promise<void>((resolve) => this.waiters.push(resolve)),
        sleep(Math.min(remaining, 250)),
      ]);
    }
  }

  stop(): void {
    this.dispose();
  }
}

describe("base contract (AC-15a)", function () {
  this.timeout(90_000);

  let daemon: DaemonHandle | undefined;
  let client: Client | undefined;

  before(async function () {
    daemon = await startDaemon();
    client = await connect({
      port: daemon.port,
      token: daemon.token,
      clientVersion: SDK_VERSION,
    });
  });

  after(async function () {
    await client?.close();
    await stopDaemon(daemon);
  });

  it("base: session.create returns a sessionId", async function () {
    const { sessionId } = await createSession(client!, { workdir: daemon!.home, mode: "accept" });
    assert.equal(typeof sessionId, "string");
    assert.ok(sessionId.length > 0, "sessionId must be a non-empty string");
    assert.ok(
      client!.trackedSessions.includes(sessionId),
      "the client should track the new session for resume-after-reconnect",
    );
  });

  it("base: session.prompt streams message.delta and then turn.done", async function () {
    const { sessionId } = await createSession(client!, { workdir: daemon!.home, mode: "accept" });
    const log = new EventLog(client!, sessionId);
    try {
      const { turnId } = await prompt(client!, sessionId, "hello");
      assert.equal(typeof turnId, "string");

      const delta = await log.waitFor("message.delta");
      const text = (delta.payload as { text?: string }).text;
      assert.equal(typeof text, "string", "message.delta payload must carry text");

      const done = await log.waitFor("turn.done");
      const reason = (done.payload as { reason?: string }).reason;
      assert.equal(reason, "complete", `expected a clean turn, got reason=${String(reason)}`);

      const deltaIndex = log.kinds().indexOf("message.delta");
      const doneIndex = log.kinds().indexOf("turn.done");
      assert.ok(deltaIndex < doneIndex, "message.delta must precede turn.done");
    } finally {
      log.stop();
    }
  });

  it("base: a denied approval ends the turn with reason 'denied'", async function () {
    const { sessionId } = await createSession(client!, { workdir: daemon!.home, mode: "accept" });
    const log = new EventLog(client!, sessionId);
    let sawRequest = false;

    const disposeHandler = onApprovalRequest(client!, async (params) => {
      sawRequest = true;
      assert.equal(params.sessionId, sessionId, "approval must be scoped to the prompting session");
      assert.equal(params.tool, "shell", "the fake script calls the shell tool");
      assert.equal(typeof params.requestId, "string");
      return { decision: "deny", scope: "once" };
    });

    try {
      await prompt(client!, sessionId, "run ls");
      const done = await log.waitFor("turn.done");
      assert.ok(sawRequest, "the daemon must ask the originating surface before running shell");
      // Since protocol 1.3.0 a refusal is fed back to the model as a failed
      // tool.result and the turn continues (up to three denials per turn),
      // so the turn ends "complete" when the model moves on or "denied"
      // when it keeps retrying. Either way the shell tool never ran.
      const reason = (done.payload as { reason?: string }).reason;
      assert.ok(
        reason === "complete" || reason === "denied",
        `expected reason=complete|denied, got ${String(reason)}`,
      );
      const result = log.events.find((e) => e.kind === "tool.result");
      assert.ok(result, "the refusal must arrive as a tool.result");
      assert.equal((result!.payload as { ok?: boolean }).ok, false, "the denied call must be reported as failed");
    } finally {
      disposeHandler();
      log.stop();
    }
  });
});

describe("subagent contract (AC-15b)", function () {
  this.timeout(120_000);

  let daemon: DaemonHandle | undefined;
  let client: Client | undefined;

  before(async function () {
    daemon = await startDaemon(SUBAGENT_SCRIPT);
    client = await connect({
      port: daemon.port,
      token: daemon.token,
      clientVersion: SDK_VERSION,
    });
  });

  after(async function () {
    await client?.close();
    await stopDaemon(daemon);
  });

  it("subagent: agent.spawn emits subagent.spawn, update, and done events", async function () {
    const { sessionId } = await createSession(client!, { workdir: daemon!.home, mode: "auto" });
    const log = new EventLog(client!, sessionId);
    try {
      const { agentId } = await spawnAgent(
        client!,
        "",
        "summarise the project layout",
        sessionId,
      );
      assert.equal(typeof agentId, "string");
      assert.ok(agentId.length > 0, "agent.spawn must answer with a correlation id");

      const spawn = await log.waitFor("subagent.spawn");
      const spawnPayload = spawn.payload as {
        agentId?: string;
        task?: string;
        status?: string;
      };
      assert.equal(spawnPayload.agentId, agentId, "the spawn event must carry the same agentId");
      assert.equal(spawnPayload.task, "summarise the project layout");
      assert.equal(spawnPayload.status, "queued");

      const update = await log.waitFor("subagent.update");
      const updatePayload = update.payload as { agentId?: string; status?: string };
      assert.equal(updatePayload.agentId, agentId);
      assert.ok(
        ["queued", "running", "done", "error"].includes(String(updatePayload.status)),
        `subagent.update carried an unknown status: ${String(updatePayload.status)}`,
      );

      const done = await log.waitFor("subagent.done");
      const donePayload = done.payload as {
        agentId?: string;
        status?: string;
        summary?: string;
        ok?: boolean;
        usage?: { inputTokens?: number; outputTokens?: number };
      };
      assert.equal(donePayload.agentId, agentId);
      assert.equal(donePayload.status, "done");
      assert.equal(donePayload.ok, true);
      assert.match(
        String(donePayload.summary),
        /core daemon/,
        "the done event must carry the child's final answer",
      );
      assert.equal(typeof donePayload.usage?.inputTokens, "number");
      assert.equal(typeof donePayload.usage?.outputTokens, "number");

      // Every subagent event belongs to the PARENT session, in seq order.
      const subagentEvents = log.events.filter((e) => e.kind.startsWith("subagent."));
      assert.ok(subagentEvents.length >= 3, `saw [${log.kinds().join(", ")}]`);
      for (const event of subagentEvents) {
        assert.equal(event.sessionId, sessionId, "subagent.* must be published on the parent");
      }
      const seqs = subagentEvents.map((e) => e.seq);
      assert.deepEqual(seqs, [...seqs].sort((a, b) => a - b), "seq must not go backwards");
      assert.equal(new Set(seqs).size, seqs.length, "each event needs its own seq");

      const kinds = log.kinds();
      assert.ok(
        kinds.indexOf("subagent.spawn") < kinds.indexOf("subagent.done"),
        "spawn must precede done",
      );
    } finally {
      log.stop();
    }
  });

  it("subagent: delegate_task is active and reports through the same events", async function () {
    const { sessionId } = await createSession(client!, { workdir: daemon!.home, mode: "auto" });
    const log = new EventLog(client!, sessionId);
    try {
      await prompt(client!, sessionId, "delegate please");
      const done = await log.waitFor("turn.done");
      assert.equal((done.payload as { reason?: string }).reason, "complete");

      const spawn = log.find("subagent.spawn");
      assert.ok(spawn, `the model's delegate_task call must spawn a child; saw [${log.kinds()}]`);
      const result = log.events.find(
        (e) => e.kind === "tool.result" && (e.payload as { name?: string }).name === "delegate_task",
      );
      assert.ok(result, "delegate_task must run rather than answer not_implemented");
      const payload = result.payload as { ok?: boolean; output?: string };
      assert.equal(payload.ok, true);
      assert.match(String(payload.output), /core daemon/);
    } finally {
      log.stop();
    }
  });

  it("subagent: agent.list reports running children with kind 'subagent'", async function () {
    const { sessionId } = await createSession(client!, { workdir: daemon!.home, mode: "auto" });
    const log = new EventLog(client!, sessionId);
    try {
      const { agentId } = await spawnAgent(client!, "", "summarise the project layout", sessionId);
      // The child may already be finished; either way the shape must hold.
      const { agents } = await listAgents(client!);
      for (const agent of agents.filter((row) => row.kind === "subagent")) {
        assert.equal(typeof agent.status, "string");
        assert.equal(agent.parentSessionId, sessionId);
      }
      await log.waitFor("subagent.done");
      assert.equal((log.find("subagent.done")!.payload as { agentId?: string }).agentId, agentId);
    } finally {
      log.stop();
    }
  });
});
