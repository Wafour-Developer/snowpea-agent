/**
 * Settings edited on disk (1.55.0): which sections wait for the person, the
 * `/settings disk` command and the two calls behind it.
 *
 * The waiting state comes from `settings.changed {waitingKeys}` and from the
 * result of `system.reloadSettings`. A core that sends neither never shows
 * anything. Only a local person may decide (`settings.acceptDiskChange`).
 */

import { useCallback, useRef, useState } from "react";

import type { TuiClient } from "../rpc/client.js";
import { parseSettingsCommand } from "../slash/settings.js";
import {
  DISK_NOTHING_WAITS,
  DISK_NOTICE_LINE,
  DISK_REQUIRES_LINE,
  normalizeWaiting,
  resultLine,
} from "../state/disk-settings.js";
import { rpcErrorText } from "../state/errors.js";

export interface DiskDeps {
  client: TuiClient;
  /** A red line in the main transcript. */
  warn: (text: string) => void;
  /** A plain line in the main transcript. */
  note: (text: string) => void;
  /** `system.reloadSettings` answered: what is degraded may have changed. */
  onReloaded?: () => void;
}

/** What deciding answers. */
export type DiskResult = { ok: true; line: string } | { ok: false; message: string };

export function useDiskSettings(deps: DiskDeps) {
  const { client, warn, note, onReloaded } = deps;
  const [waiting, setWaitingState] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const waitingRef = useRef<string[]>([]);

  const setWaiting = useCallback(
    (next: string[]) => {
      const before = waitingRef.current;
      waitingRef.current = next;
      setWaitingState(next);
      // Said once in the transcript when something starts to wait; the banner stays.
      if (before.length === 0 && next.length > 0) warn(DISK_NOTICE_LINE());
    },
    [warn],
  );

  /** `settings.changed`: a `waitingKeys` field replaces what waits; no field changes nothing. */
  const onSettingsChanged = useCallback(
    (params: { waitingKeys?: unknown }) => {
      const next = normalizeWaiting(params.waitingKeys);
      if (next !== undefined) setWaiting(next);
    },
    [setWaiting],
  );

  /** `/settings`, `/settings disk`; true when the line was one. */
  const handleCommand = useCallback(
    (line: string): boolean => {
      const command = parseSettingsCommand(line);
      if (!command) return false;
      if (command.kind === "invalid") {
        note(`Usage: ${command.usage}`);
        return true;
      }
      void client
        .call("system.reloadSettings", {})
        .then((result: { waitingKeys?: unknown } | null) => {
          onReloaded?.();
          const next = normalizeWaiting(result?.waitingKeys);
          if (next === undefined) {
            note(DISK_REQUIRES_LINE());
            return;
          }
          setWaiting(next);
          if (next.length === 0) note(DISK_NOTHING_WAITS());
          else setOpen(true);
        })
        .catch((error: unknown) => note(rpcErrorText(error)));
      return true;
    },
    [client, note, setWaiting, onReloaded],
  );

  /** `a` applies what the file says, `k` keeps the current settings. */
  const decide = useCallback(
    async (accept: boolean): Promise<DiskResult> => {
      try {
        const result = (await client.call("settings.acceptDiskChange", { accept })) as { applied?: unknown; discarded?: unknown } | null;
        setWaiting([]);
        return { ok: true, line: resultLine(result) };
      } catch (error) {
        return { ok: false, message: rpcErrorText(error) };
      }
    },
    [client, setWaiting],
  );

  return {
    waiting,
    open,
    close: useCallback(() => setOpen(false), []),
    handleCommand,
    onSettingsChanged,
    decide,
  };
}

export type DiskSettingsHook = ReturnType<typeof useDiskSettings>;
