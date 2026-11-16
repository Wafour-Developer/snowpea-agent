/**
 * Everything the security-lists screen needs from the app (capability
 * `securityLists`): the `/security` commands, the calls behind the screen, the
 * `security.blocked` notification and whether any list is in force.
 *
 * Nothing here calls the daemon unless it advertised the capability; an older
 * one gets the single "requires a newer core" line.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { CheckResult, SaveResult } from "../components/SecurityScreen.js";
import { uiLanguage } from "../layout/language.js";
import type { TuiClient } from "../rpc/client.js";
import { parseSecurityCommand } from "../slash/security.js";
import { rpcErrorFacts } from "../state/external.js";
import {
  NOTHING_RUN,
  READ_ONLY_LINE,
  SECURITY_REQUIRES_LINE,
  blockedLine,
  blockedToast,
  draftOf,
  listsAreSet,
  normalizeBlocked,
  normalizeCatalog,
  normalizeConfig,
  normalizeVerdict,
  personOnlyReadOnlyLine,
  saveProblems,
  securitySupported,
  settingsPatch,
  statusLines,
  verdictLine,
  type Lists,
  type PresetCatalog,
  type SecurityConfig,
} from "../state/security.js";
import { sanitizeText } from "../util/sanitize.js";

export interface SecurityUi {
  catalog: PresetCatalog;
  config: SecurityConfig;
  readOnly: string | null;
}

export interface SecurityDeps {
  client: TuiClient;
  workdir: string;
  /** The session the main transcript shows. */
  sessionId: string;
  /** The external session on screen, if one is open (its events show as a toast). */
  viewingId: string | null;
  showToast: (text: string) => void;
  /** A plain line in the main transcript. */
  note: (text: string) => void;
  /** A red line in the main transcript. */
  warn: (text: string) => void;
}

function errorText(error: unknown): string {
  const raw = error as { message?: string } | null;
  return sanitizeText(raw?.message ?? String(error));
}

export function useSecurityScreen(deps: SecurityDeps) {
  const { client, workdir, sessionId, viewingId, showToast, note, warn } = deps;
  const supported = securitySupported(client.serverCapabilities?.());
  const [ui, setUi] = useState<SecurityUi | null>(null);
  const [listsSet, setListsSet] = useState(false);
  const opening = useRef(false);
  /** Counts `settings.changed` events that touched the security settings; an open screen follows it. */
  const [changeSeq, setChangeSeq] = useState(0);
  const openRef = useRef(false);
  openRef.current = ui !== null;
  const catalogRef = useRef<PresetCatalog | null>(null);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const viewingRef = useRef(viewingId);
  viewingRef.current = viewingId;

  const loadCatalog = useCallback(async (): Promise<PresetCatalog> => {
    const catalog = normalizeCatalog(await client.call("security.presets.list", { locale: uiLanguage() }));
    catalogRef.current = catalog;
    return catalog;
  }, [client]);

  const loadConfig = useCallback(async (): Promise<SecurityConfig> => {
    const config = normalizeConfig(await client.call("security.get", {}));
    setListsSet(listsAreSet(config));
    return config;
  }, [client]);

  // Whether any list is in force decides the "does not apply to this session" lines.
  const refreshListsSet = useCallback(() => {
    if (!supported) return;
    void loadConfig().catch(() => undefined);
  }, [supported, loadConfig]);
  useEffect(() => {
    refreshListsSet();
  }, [refreshListsSet]);

  const open = useCallback(async () => {
    if (opening.current) return;
    opening.current = true;
    showToast("Loading security lists…");
    try {
      const [catalog, config] = await Promise.all([loadCatalog(), loadConfig()]);
      setUi({ catalog, config, readOnly: config.writable ? null : READ_ONLY_LINE });
      setChangeSeq(0);
    } catch (error) {
      note(`Security lists: could not load them — ${errorText(error)}`);
    } finally {
      opening.current = false;
    }
  }, [loadCatalog, loadConfig, note, showToast]);

  const save = useCallback(
    async (lists: Lists): Promise<SaveResult> => {
      try {
        await client.call("settings.set", { scope: "global", patch: settingsPatch(lists) });
      } catch (error) {
        if (rpcErrorFacts(error).code === "jarvis_person_only") {
          return { ok: false, message: errorText(error), readOnly: personOnlyReadOnlyLine(error) };
        }
        // `invalid_params` carries every reason in `details.problems`; the ones that name an entry go on its row.
        if (rpcErrorFacts(error).code === "invalid_params") {
          const { entries, others } = saveProblems(error);
          return { ok: false, message: errorText(error), entries, others };
        }
        return { ok: false, message: errorText(error) };
      }
      try {
        return { ok: true, config: await loadConfig() };
      } catch (error) {
        return { ok: false, message: `saved, but could not read the lists back — ${errorText(error)}` };
      }
    },
    [client, loadConfig],
  );

  const check = useCallback(
    async (what: "command" | "path", text: string, draft: Lists | null = null): Promise<CheckResult> => {
      try {
        // `draft` judges the edits not saved yet instead of the saved lists.
        const raw = await client.call("security.check", {
          [what]: text,
          workdir,
          locale: uiLanguage(),
          ...(draft ? { draft: draftOf(draft) } : {}),
        });
        return normalizeVerdict(raw);
      } catch (error) {
        return { error: errorText(error) };
      }
    },
    [client, workdir],
  );

  /** `/security`, `/security status`, `/security check …`; true when the line was one. */
  const handleCommand = useCallback(
    (line: string): boolean => {
      const command = parseSecurityCommand(line);
      if (!command) return false;
      if (!supported) {
        note(SECURITY_REQUIRES_LINE);
        return true;
      }
      if (command.kind === "invalid") {
        note(`Usage: ${command.usage}`);
      } else if (command.kind === "enter") {
        void open();
      } else if (command.kind === "status") {
        void Promise.all([loadConfig(), catalogRef.current ? Promise.resolve(catalogRef.current) : loadCatalog().catch(() => null)])
          .then(([config, catalog]) => note(statusLines(config, catalog).join("\n")))
          .catch((error: unknown) => note(`Security lists: could not read them — ${errorText(error)}`));
      } else {
        void Promise.all([check(command.what, command.text), catalogRef.current ? Promise.resolve(catalogRef.current) : loadCatalog().catch(() => null)]).then(
          ([result, catalog]) => {
            if ("error" in result) {
              note(`Security check failed — ${result.error}`);
              return;
            }
            const verdict = verdictLine(result, catalog);
            const text = `${verdict.text}\n${NOTHING_RUN}`;
            if (verdict.blocked) warn(text);
            else note(text);
          },
        );
      }
      return true;
    },
    [check, loadCatalog, loadConfig, note, open, supported, warn],
  );

  /** `security.blocked`: a red line in the session on screen, a toast for any other. */
  const onBlocked = useCallback(
    (raw: unknown) => {
      const event = normalizeBlocked(raw as never);
      if (event.summary === "") return;
      if (event.sessionId === sessionIdRef.current) warn(blockedLine(event));
      else showToast(blockedToast(event, event.sessionId === viewingRef.current));
    },
    [showToast, warn],
  );

  const onSettingsChanged = useCallback(
    (params: { keys?: string[] }) => {
      if (!params.keys || params.keys.some((key) => key.startsWith("security"))) {
        refreshListsSet();
        if (openRef.current) setChangeSeq((seq) => seq + 1);
      }
    },
    [refreshListsSet],
  );

  return {
    supported,
    ui,
    close: useCallback(() => setUi(null), []),
    handleCommand,
    save,
    check,
    reload: loadConfig,
    changeSeq,
    onBlocked,
    onSettingsChanged,
    /** True while lists are in force, so ungated sessions can say they are not covered. */
    listsSet: supported && listsSet,
  };
}

export type SecurityScreenHook = ReturnType<typeof useSecurityScreen>;
