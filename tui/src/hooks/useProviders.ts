/**
 * `/providers` (capability `hiddenModels`, protocol 1.60.0; disconnecting
 * works on every daemon): the calls behind {@link ProvidersScreen}.
 *
 * The screen has three stages: the provider list, one provider's models, and
 * the confirmation before a provider is disconnected. Every change goes to the
 * daemon and the list is read again, so what is shown is what was saved.
 */

import { useCallback, useRef, useState } from "react";

import type { TuiClient } from "../rpc/client.js";
import { rpcErrorText } from "../state/errors.js";
import {
  disconnectedLine,
  hiddenModelsSupported,
  normalizeProviders,
  parseProvidersCommand,
  removedLine,
  unhiddenLine,
  type ProviderRow,
} from "../state/providers.js";

export type ProvidersUi =
  | { stage: "list"; providers: ProviderRow[] }
  | { stage: "provider"; providers: ProviderRow[]; vendor: string; models: string[] }
  | { stage: "confirm"; providers: ProviderRow[]; vendor: string; models: string[] };

export interface ProvidersDeps {
  client: TuiClient;
  /** A plain line in the main transcript. */
  note: (text: string) => void;
  showToast: (text: string) => void;
}

export function useProviders(deps: ProvidersDeps) {
  const { client, note, showToast } = deps;
  const supported = hiddenModelsSupported(client.serverCapabilities?.());
  const [ui, setUi] = useState<ProvidersUi | null>(null);
  const busy = useRef(false);

  const listProviders = useCallback(
    async () => normalizeProviders(await client.call("provider.list", {})),
    [client],
  );

  const modelsOf = useCallback(
    async (row: ProviderRow | undefined): Promise<string[]> => {
      if (!row || !supported) return [];
      try {
        const result = (await client.call("provider.models", { vendor: row.vendor })) as { models?: unknown };
        return Array.isArray(result?.models) ? result.models.filter((m): m is string => typeof m === "string") : row.models;
      } catch {
        return row.models;
      }
    },
    [client, supported],
  );

  const showProvider = useCallback(
    async (vendor: string, providers?: ProviderRow[]) => {
      const rows = providers ?? (await listProviders());
      const row = rows.find((item) => item.vendor === vendor);
      if (!row) {
        setUi({ stage: "list", providers: rows });
        return;
      }
      setUi({ stage: "provider", providers: rows, vendor, models: await modelsOf(row) });
    },
    [listProviders, modelsOf],
  );

  const run = useCallback(
    async (work: () => Promise<void>) => {
      if (busy.current) return;
      busy.current = true;
      try {
        await work();
      } catch (error) {
        note(rpcErrorText(error));
      } finally {
        busy.current = false;
      }
    },
    [note],
  );

  const open = useCallback(
    (vendor: string) =>
      run(async () => {
        const providers = await listProviders();
        if (vendor) await showProvider(vendor, providers);
        else setUi({ stage: "list", providers });
      }),
    [listProviders, run, showProvider],
  );

  /** `/providers [vendor]`; true when the line was one. */
  const handleCommand = useCallback(
    (line: string): boolean => {
      const command = parseProvidersCommand(line);
      if (!command) return false;
      void open(command.vendor);
      return true;
    },
    [open],
  );

  const removeModel = useCallback(
    (vendor: string, model: string) =>
      run(async () => {
        const result = (await client.call("provider.model.remove", { vendor, model })) as { action?: unknown };
        const action = typeof result?.action === "string" ? result.action : "hidden";
        note(removedLine(vendor, model, action));
        showToast(removedLine(vendor, model, action));
        await showProvider(vendor);
      }),
    [client, note, run, showProvider, showToast],
  );

  const unhideModel = useCallback(
    (vendor: string, model: string) =>
      run(async () => {
        await client.call("provider.model.unhide", { vendor, model });
        note(unhiddenLine(vendor, model));
        await showProvider(vendor);
      }),
    [client, note, run, showProvider],
  );

  const disconnect = useCallback(
    (vendor: string) =>
      run(async () => {
        await client.call("provider.remove", { vendor });
        note(disconnectedLine(vendor));
        setUi({ stage: "list", providers: await listProviders() });
      }),
    [client, listProviders, note, run],
  );

  const askDisconnect = useCallback(() => {
    setUi((current) => (current && current.stage === "provider" ? { ...current, stage: "confirm" } : current));
  }, []);

  const back = useCallback(() => {
    setUi((current) => {
      if (!current) return null;
      if (current.stage === "confirm") return { ...current, stage: "provider" };
      if (current.stage === "provider") return { stage: "list", providers: current.providers };
      return null;
    });
  }, []);

  return {
    supported,
    ui,
    close: useCallback(() => setUi(null), []),
    back,
    handleCommand,
    showProvider: useCallback((vendor: string) => void run(() => showProvider(vendor)), [run, showProvider]),
    removeModel,
    unhideModel,
    askDisconnect,
    disconnect,
  };
}

export type ProvidersHook = ReturnType<typeof useProviders>;
