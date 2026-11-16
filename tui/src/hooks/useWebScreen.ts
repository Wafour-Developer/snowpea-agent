/**
 * Everything the Remote access screen needs from the app (capability
 * `webRemoteControl`): the `/web` commands, the calls behind the screen and
 * copying the access link to the clipboard.
 *
 * Nothing here calls the daemon unless it advertised the capability; an older
 * one gets the single "requires a newer core" line. Only a local person may use
 * any of it, so a `jarvis_person_only` answer is shown as such and the screen
 * does not open.
 */

import { useCallback, useRef, useState } from "react";

import { parseWebCommand } from "../slash/web.js";
import { rpcCode } from "../state/errors.js";
import {
  WEB_REQUIRES_LINE,
  listenParams,
  localPersonOnlyLine,
  normalizeStatus,
  osc52Sequence,
  osc52Supported,
  summaryLines,
  webRemoteSupported,
  type ListenForm,
  type WebStatus,
} from "../state/web-remote.js";
import type { TuiClient } from "../rpc/client.js";
import { sanitizeText } from "../util/sanitize.js";

export interface WebUi {
  status: WebStatus;
}

export interface WebDeps {
  client: TuiClient;
  /** A plain line in the main transcript. */
  note: (text: string) => void;
  showToast: (text: string) => void;
  /** Writes to the terminal (the OSC 52 sequence goes here). */
  write: (data: string) => void;
}

/** What a call behind the screen answers: the status as it is now, or the refusal as it came. */
export type WebResult = { ok: true; status: WebStatus; link?: string | null } | { ok: false; error: unknown };

export type CopyResult = "copied" | "unsupported";

function errorText(error: unknown): string {
  const raw = error as { message?: string } | null;
  return sanitizeText(raw?.message ?? String(error));
}

export function useWebScreen(deps: WebDeps) {
  const { client, note, showToast, write } = deps;
  const supported = webRemoteSupported(client.serverCapabilities?.());
  const [ui, setUi] = useState<WebUi | null>(null);
  const opening = useRef(false);
  /** Counts `settings.changed` events that touched `web`; an open screen reads the status again. */
  const [changeSeq, setChangeSeq] = useState(0);
  const openRef = useRef(false);
  openRef.current = ui !== null;

  const read = useCallback(async (): Promise<WebStatus> => normalizeStatus(await client.call("web.remote.status", {})), [client]);

  /** A `web.remote.*` call whose answer is the status. */
  const run = useCallback(
    async (method: string, params: Record<string, unknown> = {}): Promise<WebResult> => {
      try {
        const raw = await client.call(method, params);
        // Every call but rotateToken answers the status; the other keeps the link only.
        if (method === "web.remote.rotateToken") {
          const status = await read();
          const link = typeof raw?.accessUrl === "string" && raw.accessUrl !== "" ? sanitizeText(raw.accessUrl) : status.accessUrl;
          return { ok: true, status: { ...status, accessUrl: link }, link };
        }
        return { ok: true, status: normalizeStatus(raw) };
      } catch (error) {
        return { ok: false, error };
      }
    },
    [client, read],
  );

  const open = useCallback(async () => {
    if (opening.current) return;
    opening.current = true;
    showToast("Loading remote access…");
    try {
      setUi({ status: await read() });
      setChangeSeq(0);
    } catch (error) {
      note(rpcCode(error) === "jarvis_person_only" ? localPersonOnlyLine(error) : `Remote access: could not read it — ${errorText(error)}`);
    } finally {
      opening.current = false;
    }
  }, [note, read, showToast]);

  /** `/web`, `/web status`; true when the line was one. */
  const handleCommand = useCallback(
    (line: string): boolean => {
      const command = parseWebCommand(line);
      if (!command) return false;
      if (!supported) {
        note(WEB_REQUIRES_LINE);
        return true;
      }
      if (command.kind === "invalid") {
        note(`Usage: ${command.usage}`);
      } else if (command.kind === "enter") {
        void open();
      } else {
        void read()
          .then((status) => note(summaryLines(status).join("\n")))
          .catch((error: unknown) =>
            note(rpcCode(error) === "jarvis_person_only" ? localPersonOnlyLine(error) : `Remote access: could not read it — ${errorText(error)}`),
          );
      }
      return true;
    },
    [note, open, read, supported],
  );

  const listen = useCallback((form: ListenForm) => run("web.remote.listen", listenParams(form)), [run]);
  const stop = useCallback((save: boolean) => run("web.remote.stop", save ? { save: true } : {}), [run]);
  const rotate = useCallback(() => run("web.remote.rotateToken"), [run]);
  const setPassword = useCallback(
    (password: string, username: string) => run("web.remote.setPassword", { password, ...(username.trim() !== "" ? { username: username.trim() } : {}) }),
    [run],
  );
  const clearPassword = useCallback(() => run("web.remote.clearPassword"), [run]);
  const refresh = useCallback(async (): Promise<WebResult> => {
    try {
      return { ok: true, status: await read() };
    } catch (error) {
      return { ok: false, error };
    }
  }, [read]);

  /** Put the link on the clipboard when this terminal obeys OSC 52; otherwise say so, and the screen prints it once. */
  const copy = useCallback(
    (link: string): CopyResult => {
      if (!osc52Supported(process.env)) return "unsupported";
      write(osc52Sequence(link));
      return "copied";
    },
    [write],
  );

  const onSettingsChanged = useCallback((params: { keys?: string[] }) => {
    if (!openRef.current) return;
    if (!params.keys || params.keys.some((key) => key.startsWith("web"))) setChangeSeq((seq) => seq + 1);
  }, []);

  return {
    supported,
    ui,
    close: useCallback(() => setUi(null), []),
    handleCommand,
    listen,
    stop,
    rotate,
    setPassword,
    clearPassword,
    refresh,
    copy,
    changeSeq,
    onSettingsChanged,
  };
}

export type WebScreenHook = ReturnType<typeof useWebScreen>;
