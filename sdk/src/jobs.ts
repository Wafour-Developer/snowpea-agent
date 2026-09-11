/**
 * Scheduler helpers. Like `agents.ts`, the schema is frozen at M1 while the
 * daemon still answers `error{code:"not_implemented"}` until the scheduler ships.
 */

import type { Client } from "./client.js";
import type { MethodMap } from "./protocol.js";

type P<M extends keyof MethodMap> = MethodMap[M]["params"];
type R<M extends keyof MethodMap> = MethodMap[M]["result"];

/** Schedule a task with a cron or natural-language spec. */
export function scheduleJob(client: Client, params: P<"job.schedule">): Promise<R<"job.schedule">> {
  return client.call("job.schedule", params);
}

/** List scheduled jobs. */
export function listJobs(client: Client): Promise<R<"job.list">> {
  return client.call("job.list", {} as P<"job.list">);
}

/** Cancel a scheduled job. */
export function cancelJob(client: Client, jobId: string): Promise<R<"job.cancel">> {
  return client.call("job.cancel", { jobId } as P<"job.cancel">);
}

/** Run a scheduled job immediately, without changing its schedule. */
export function runJobNow(client: Client, jobId: string): Promise<R<"job.runNow">> {
  return client.call("job.runNow", { jobId } as P<"job.runNow">);
}
