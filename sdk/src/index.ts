/**
 * `@snowpea/sdk` — TypeScript client for the snowpea daemon.
 *
 * The daemon owns every piece of state; this package is the one way the Ink TUI,
 * the headless CLI, and (from v0.2) the desktop app talk to it. Protocol types
 * live in the generated `protocol.ts` and must not be hand-edited.
 *
 * ```ts
 * import { connect, createSession, prompt } from "@snowpea/sdk";
 *
 * const client = await connect({ port, token, clientVersion: "0.1.0" });
 * client.on("session.event", (e) => console.log(e.kind, e.payload));
 * const { sessionId } = await createSession(client, { workdir: process.cwd() });
 * await prompt(client, sessionId, "hello");
 * ```
 */

export const SDK_VERSION = "0.1.0";

export * from "./protocol.js";
export * from "./client.js";
export * from "./sessions.js";
export * from "./approvals.js";
export * from "./agents.js";
export * from "./jobs.js";
export * from "./tools.js";

import { connect } from "./client.js";

export default { SDK_VERSION, connect };
