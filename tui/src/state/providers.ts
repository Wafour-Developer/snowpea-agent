/**
 * `/providers`: the connected providers, their models, and taking one away
 * (capability `hiddenModels`, protocol 1.60.0).
 *
 * A provider's model list is mostly what its endpoint serves, so "remove this
 * model" means hiding it from every picker (`provider.model.remove` answers
 * `hidden`), unless the id was added by hand to settings, in which case it is
 * taken off that list (`unlisted`). Hidden models can be shown again, and one
 * that something still uses (`hiddenInUse`) keeps working and is flagged.
 *
 * Disconnecting a provider (`provider.remove`) predates the capability and is
 * offered to every daemon; the model rows need `hiddenModels`.
 *
 * Pure, so the tests read the same functions the screen draws.
 */

import type { CommandInfo } from "../rpc/sdk.js";
import { say } from "./errors.js";
import { sanitizeText } from "../util/sanitize.js";

export function hiddenModelsSupported(capabilities: readonly string[] | undefined): boolean {
  return (capabilities ?? []).includes("hiddenModels");
}

export const PROVIDERS_COMMAND: CommandInfo = {
  name: "providers",
  summary: "Connected providers: disconnect one, remove a model from its list, show hidden models again.",
  source: "tui",
};

/** `/providers [vendor]`; `null` when the line is something else. */
export function parseProvidersCommand(line: string): { vendor: string } | null {
  const match = /^\/providers(?:\s+(\S+))?\s*$/u.exec(line.trim());
  if (!match) return null;
  return { vendor: match[1] ?? "" };
}

export interface HiddenUse {
  model: string;
  usedBy: string[];
}

export interface ProviderRow {
  vendor: string;
  label: string;
  configured: boolean;
  isDefault: boolean;
  custom: boolean;
  defaultModel: string;
  models: string[];
  hiddenModels: string[];
  hiddenInUse: HiddenUse[];
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

/** `provider.list` → rows; only the providers a person has connected (or added). */
export function normalizeProviders(result: unknown): ProviderRow[] {
  const raw = (result as { providers?: unknown } | null)?.providers;
  const rows: ProviderRow[] = [];
  for (const item of Array.isArray(raw) ? raw : []) {
    if (!item || typeof item !== "object") continue;
    const entry = item as Record<string, unknown>;
    const vendor = typeof entry.vendor === "string" ? entry.vendor : "";
    if (!vendor) continue;
    const uses: HiddenUse[] = [];
    for (const use of Array.isArray(entry.hiddenInUse) ? entry.hiddenInUse : []) {
      const model = (use as { model?: unknown })?.model;
      if (typeof model === "string") uses.push({ model, usedBy: strings((use as { usedBy?: unknown }).usedBy) });
    }
    const row: ProviderRow = {
      vendor,
      label: typeof entry.label === "string" && entry.label ? entry.label : vendor,
      configured: entry.configured === true,
      isDefault: entry.default === true,
      custom: entry.custom === true,
      defaultModel: typeof entry.defaultModel === "string" ? entry.defaultModel : "",
      models: strings(entry.models),
      hiddenModels: strings(entry.hiddenModels),
      hiddenInUse: uses,
    };
    if (row.configured || row.custom || row.hiddenModels.length > 0) rows.push(row);
  }
  return rows;
}

/** One `usedBy` value in words: `default`, `category:fast`, `agent:x`, `session:<id>`. */
export function usedByText(value: string): string {
  if (value === "default") return say("the default model", "기본 모델");
  const [kind, ...rest] = value.split(":");
  const name = sanitizeText(rest.join(":"));
  if (kind === "category") return say(`the ${name} slot`, `${name} 슬롯`);
  if (kind === "agent") return say(`agent ${name}`, `에이전트 ${name}`);
  if (kind === "session") return say("a running session", "실행 중인 세션");
  return sanitizeText(value);
}

/** The warning for each hidden model that something still uses. */
export function hiddenInUseLines(row: ProviderRow): string[] {
  return row.hiddenInUse
    .filter((use) => use.usedBy.length > 0)
    .map((use) => {
      const who = use.usedBy.map(usedByText).join(", ");
      const model = sanitizeText(use.model);
      return say(
        `⚠ ${model} is hidden but still used by ${who}; it keeps working.`,
        `⚠ ${model} 은(는) 숨겼지만 ${who}에서 아직 쓰고 있어요. 계속 동작해요.`,
      );
    });
}

/** What the person reads before a provider is disconnected. */
export function disconnectConfirmText(row: Pick<ProviderRow, "vendor" | "label">): string {
  const name = sanitizeText(row.label || row.vendor);
  return say(
    `Disconnect ${name}? Its API key or login is removed from this machine, and the model slots and agents that used it fall back to the default model.`,
    `${name} 연결을 끊을까요? 이 컴퓨터에서 API 키나 로그인이 지워지고, 이 제공자를 쓰던 모델 슬롯과 에이전트는 기본 모델로 돌아가요.`,
  );
}

/** The line left in the transcript after `provider.model.remove`. */
export function removedLine(vendor: string, model: string, action: string): string {
  const id = sanitizeText(model);
  const name = sanitizeText(vendor);
  if (action === "unlisted") {
    return say(
      `${id} was added by hand to ${name}; it is taken off that list.`,
      `${id} 은(는) ${name}에 직접 추가한 모델이라 그 목록에서 뺐어요.`,
    );
  }
  return say(
    `${id} is hidden from ${name}'s model lists. /providers ${name} shows it again.`,
    `${id} 을(를) ${name} 모델 목록에서 숨겼어요. /providers ${name} 에서 다시 보이게 할 수 있어요.`,
  );
}

export const unhiddenLine = (vendor: string, model: string): string =>
  say(
    `${sanitizeText(model)} is back in ${sanitizeText(vendor)}'s model lists.`,
    `${sanitizeText(model)} 을(를) ${sanitizeText(vendor)} 모델 목록에 다시 보이게 했어요.`,
  );

export const disconnectedLine = (vendor: string): string =>
  say(`${sanitizeText(vendor)} is disconnected.`, `${sanitizeText(vendor)} 연결을 끊었어요.`);

/** What a row on the provider list says beside its name. */
export function providerBadges(row: ProviderRow): string {
  const parts: string[] = [];
  if (row.isDefault) parts.push(say("default", "기본"));
  if (!row.configured) parts.push(say("not connected", "연결 안 됨"));
  if (row.hiddenModels.length > 0) {
    parts.push(say(`${row.hiddenModels.length} hidden`, `${row.hiddenModels.length}개 숨김`));
  }
  if (row.hiddenInUse.some((use) => use.usedBy.length > 0)) parts.push("⚠");
  return parts.join(" · ");
}

/** One row of a provider's screen. */
export type ProviderAction =
  | { kind: "remove"; model: string }
  | { kind: "unhide"; model: string }
  | { kind: "disconnect" }
  | { kind: "back" };

export interface ProviderMenuRow {
  label: string;
  hint?: string;
  danger?: boolean;
  action: ProviderAction;
}

/**
 * A provider's rows: its models (Enter removes one), its hidden models (Enter
 * shows one again), then Disconnect. Without `hiddenModels` only Disconnect.
 */
export function providerMenu(row: ProviderRow, models: string[], supported: boolean): ProviderMenuRow[] {
  const rows: ProviderMenuRow[] = [];
  if (supported) {
    const used = new Map(row.hiddenInUse.map((use) => [use.model, use.usedBy]));
    for (const model of models) {
      rows.push({
        label: sanitizeText(model),
        hint: model === row.defaultModel ? say("in use · Enter removes it from the list", "사용 중 · Enter 로 목록에서 빼기") : undefined,
        action: { kind: "remove", model },
      });
    }
    for (const model of row.hiddenModels) {
      const who = used.get(model) ?? [];
      rows.push({
        label: say(`${sanitizeText(model)} (hidden)`, `${sanitizeText(model)} (숨김)`),
        hint:
          who.length > 0
            ? say(`still used by ${who.map(usedByText).join(", ")} · Enter shows it again`, `${who.map(usedByText).join(", ")}에서 사용 중 · Enter 로 다시 보이기`)
            : say("Enter shows it again", "Enter 로 다시 보이기"),
        action: { kind: "unhide", model },
      });
    }
  }
  if (row.configured || row.custom) {
    rows.push({
      label: say(`Disconnect ${sanitizeText(row.label)}`, `${sanitizeText(row.label)} 연결 끊기`),
      danger: true,
      action: { kind: "disconnect" },
    });
  }
  rows.push({ label: say("Back", "뒤로"), action: { kind: "back" } });
  return rows;
}
