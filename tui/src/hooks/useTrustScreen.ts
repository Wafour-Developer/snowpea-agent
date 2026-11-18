/**
 * Everything the project-trust screen needs from the app (capability
 * `projectTrust`): the `/trust` commands, the calls behind the screen and the
 * `project.trust.changed` notification.
 *
 * Nothing here calls the daemon unless it advertised the capability; an older
 * one gets the single "requires a newer core" line. Trusting sends back exactly
 * the digests of the status that was shown (`project.trust.set`); when the disk
 * moved meanwhile core refuses with `trust_changed` and nothing is trusted.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { TuiClient } from "../rpc/client.js";
import { parseTrustCommand } from "../slash/trust.js";
import { rpcCode } from "../state/errors.js";
import {
  STORE_LOST_LINE,
  TAMPER_LINE,
  TRUST_REQUIRES_LINE,
  changedFiles,
  changedLines,
  concerns,
  initialSelection,
  normalizeChanged,
  normalizeTrust,
  setParams,
  statusLine,
  trustRefusalLine,
  trustSupported,
  type ChangedFiles,
  type TrustScope,
  type TrustView,
} from "../state/trust.js";
import { say } from "../state/errors.js";
import { sanitizeText } from "../util/sanitize.js";

export interface TrustUi {
  status: TrustView;
}

/** The startup question for an untrusted folder; `note` is why the last answer did not take. */
export interface TrustGate {
  status: TrustView;
  busy: boolean;
  note: string | null;
  /** Core refused trust from this client: the person may only go on without it. */
  refused: boolean;
}

export interface TrustDeps {
  client: TuiClient;
  workdir: string;
  /** A plain line in the main transcript. */
  note: (text: string) => void;
  /** A red line in the main transcript. */
  warn?: (text: string) => void;
  showToast: (text: string) => void;
}

/** What a call behind the screen answers. */
export type TrustResult =
  | { ok: true; status: TrustView }
  /** Core refused: a person on this machine only (`jarvis_person_only`). */
  | { ok: false; kind: "refused"; message: string }
  /** The files moved after they were shown; nothing was trusted and `status` is the reloaded one. */
  | { ok: false; kind: "changed"; files: ChangedFiles; status: TrustView | null }
  | { ok: false; kind: "error"; message: string };

function errorText(error: unknown): string {
  const raw = error as { message?: string } | null;
  return sanitizeText(raw?.message ?? String(error));
}

export function useTrustScreen(deps: TrustDeps) {
  const { client, workdir, note, showToast } = deps;
  const warn = deps.warn ?? note;
  const supported = trustSupported(client.serverCapabilities?.());
  const [ui, setUi] = useState<TrustUi | null>(null);
  const opening = useRef(false);
  /** Counts `project.trust.changed` events that concern this project; an open screen reads the status again. */
  const [changeSeq, setChangeSeq] = useState(0);
  /** Something outside snowpea tried to change the trust records (`reason: "tamper"`); the open screen says so. */
  const [tampered, setTampered] = useState(false);
  const openRef = useRef(false);
  openRef.current = ui !== null;
  const [gate, setGate] = useState<TrustGate | null>(null);

  // At startup: an untrusted folder is asked about once, before anything else.
  useEffect(() => {
    if (!supported) return;
    let live = true;
    void client
      .call("project.trust.status", { workdir })
      .then((raw) => {
        const status = normalizeTrust(raw);
        if (live && status.workdir !== "" && !status.trusted) setGate({ status, busy: false, note: null, refused: false });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [client, supported, workdir]);

  const read = useCallback(async (): Promise<TrustView> => normalizeTrust(await client.call("project.trust.status", { workdir })), [client, workdir]);

  const open = useCallback(async () => {
    if (opening.current) return;
    opening.current = true;
    showToast(say("Loading project trust…", "프로젝트 신뢰 정보를 불러오는 중…"));
    try {
      setUi({ status: await read() });
      setChangeSeq(0);
      setTampered(false);
    } catch (error) {
      note(`${say("Project trust: could not read it", "프로젝트 신뢰: 읽을 수 없습니다")} — ${errorText(error)}`);
    } finally {
      opening.current = false;
    }
  }, [note, read, showToast]);

  /** `/trust`, `/trust status`; true when the line was one. */
  const handleCommand = useCallback(
    (line: string): boolean => {
      const command = parseTrustCommand(line);
      if (!command) return false;
      if (!supported) {
        note(TRUST_REQUIRES_LINE);
        return true;
      }
      if (command.kind === "invalid") {
        note(`Usage: ${command.usage}`);
      } else if (command.kind === "enter") {
        void open();
      } else {
        void read()
          .then((status) => note(statusLine(status)))
          .catch((error: unknown) => note(`${say("Project trust: could not read it", "프로젝트 신뢰: 읽을 수 없습니다")} — ${errorText(error)}`));
      }
      return true;
    },
    [note, open, read, supported],
  );

  const refresh = useCallback(async (): Promise<TrustResult> => {
    try {
      return { ok: true, status: await read() };
    } catch (error) {
      return { ok: false, kind: "error", message: errorText(error) };
    }
  }, [read]);

  /** A `set` / `revoke` call: success is the new status, a refusal is told in words. */
  const change = useCallback(
    async (method: "project.trust.set" | "project.trust.revoke", params: Record<string, unknown>): Promise<TrustResult> => {
      try {
        return { ok: true, status: normalizeTrust(await client.call(method, params)) };
      } catch (error) {
        const code = rpcCode(error);
        if (code === "jarvis_person_only") return { ok: false, kind: "refused", message: trustRefusalLine(error) };
        if (code === "trust_changed") {
          const reloaded = await read().catch(() => null);
          return { ok: false, kind: "changed", files: changedFiles(error), status: reloaded };
        }
        return { ok: false, kind: "error", message: errorText(error) };
      }
    },
    [client, read],
  );

  /** Trust the scopes of the status that was shown, with exactly its digests. */
  const trust = useCallback(
    (shown: TrustView, selected: ReadonlySet<TrustScope>) => change("project.trust.set", setParams(shown, selected)),
    [change],
  );

  /** Take the scopes back; `null` means every scope. */
  const revoke = useCallback(
    (scopes: TrustScope[] | null) => change("project.trust.revoke", { workdir, ...(scopes ? { scopes } : {}) }),
    [change, workdir],
  );

  const onChanged = useCallback(
    (raw: unknown) => {
      const event = normalizeChanged(raw);
      if (event.reason === "store_lost") showToast(STORE_LOST_LINE());
      if (event.reason === "tamper") {
        // Nothing about any project's trust changed: a warning, not a trust change.
        setTampered(true);
        warn(TAMPER_LINE());
        return;
      }
      if (openRef.current && concerns(event, workdir)) setChangeSeq((seq) => seq + 1);
    },
    [showToast, warn, workdir],
  );

  /** "Yes, trust this folder": the default scopes, with exactly the digests that were shown. */
  const acceptGate = useCallback(async () => {
    if (!gate || gate.busy) return;
    const shown = gate.status;
    setGate({ ...gate, busy: true, note: null });
    try {
      await client.call("project.trust.set", setParams(shown, initialSelection(shown)));
      setGate(null);
    } catch (error) {
      const code = rpcCode(error);
      if (code === "jarvis_person_only") {
        setGate({ status: shown, busy: false, note: trustRefusalLine(error), refused: true });
      } else if (code === "trust_changed") {
        const reloaded = await read().catch(() => null);
        setGate({ status: reloaded ?? shown, busy: false, note: changedLines(changedFiles(error))[0] ?? null, refused: false });
      } else {
        setGate({ status: shown, busy: false, note: errorText(error), refused: false });
      }
    }
  }, [client, gate, read]);

  return {
    supported,
    gate,
    acceptGate,
    dismissGate: useCallback(() => setGate(null), []),
    ui,
    close: useCallback(() => setUi(null), []),
    handleCommand,
    refresh,
    trust,
    revoke,
    changeSeq,
    tampered,
    onChanged,
  };
}

export type TrustScreenHook = ReturnType<typeof useTrustScreen>;
