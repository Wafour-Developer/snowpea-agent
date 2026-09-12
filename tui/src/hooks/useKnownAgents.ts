/**
 * The agents the daemon has definitions for.
 *
 * `agent.list` answers with every named agent, whether or not anything is
 * running it, which is what lets the panel show an idle roster rather than only
 * the delegates of the current turn. It changes when a definition file is added
 * or a plugin is installed, so a slow poll is enough.
 */

import { useEffect, useState } from "react";

import type { KnownAgent } from "../layout/agents.js";

/** Structural subset of `TuiClient`, so tests can pass a stub. */
export interface AgentListClient {
  call(method: string, params?: Record<string, unknown>): Promise<any>;
}

export const AGENT_LIST_POLL_MS = 30_000;

const EMPTY: KnownAgent[] = [];

export function useKnownAgents(
  client: AgentListClient,
  pollMs: number = AGENT_LIST_POLL_MS,
): KnownAgent[] {
  const [agents, setAgents] = useState<KnownAgent[]>(EMPTY);

  useEffect(() => {
    let cancelled = false;
    const read = (): void => {
      void client
        .call("agent.list", {})
        .then((result) => {
          if (cancelled) return;
          const next = Array.isArray(result?.agents) ? (result.agents as KnownAgent[]) : EMPTY;
          // Same names in the same order means the same panel; keeping the
          // array identity keeps the panel's memo from repainting.
          setAgents((current) =>
            current.length === next.length &&
            current.every((agent, index) => agent.name === next[index]?.name)
              ? current
              : next,
          );
        })
        .catch(() => {
          /* advisory: an older daemon may not know the method at all. */
        });
    };
    read();
    const timer = setInterval(read, pollMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [client, pollMs]);

  return agents;
}
