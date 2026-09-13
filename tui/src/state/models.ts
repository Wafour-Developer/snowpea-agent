/**
 * The list `/model` offers to pick from.
 *
 * Three sources, in the order a person would want them: the named profiles the
 * settings define, whatever the vendor's endpoint actually serves, and the
 * model in use right now. They are merged rather than shown as three lists,
 * because from the outside they are all just "what can this session talk to" —
 * but each row says where it came from, since a profile and a raw model id
 * behave differently when the session is restarted.
 *
 * Pure, so `test/models.test.ts` can check the merge without a daemon.
 */

/** Where an option came from. */
export type ModelOrigin = "profile" | "discovered" | "current";

export interface ModelOption {
  /** What `/model <ref>` is called with. */
  ref: string;
  /** Row text. */
  label: string;
  /** `anthropic` · `claude-sonnet-4-5`, when the source says. */
  detail: string;
  origin: ModelOrigin;
  /** True for the model the session is using now. */
  current: boolean;
}

/** One entry of `models.profiles` in the settings document. */
export interface ModelProfile {
  provider?: string;
  model?: string;
}

export interface ModelPickerInput {
  /** `models.profiles` from `settings.get`. */
  profiles?: Record<string, ModelProfile> | null;
  /** `models.default`, marked in the listing. */
  defaultProfile?: string | null;
  /** `agents.models`: which profile each named agent uses. */
  agentModels?: Record<string, string> | null;
  /** `provider.models` — what the vendor's endpoint reports. */
  discovered?: string[] | null;
  /** The model the session is on. */
  current?: string | null;
  /** The vendor serving it. */
  vendor?: string | null;
}

/**
 * Every option, profiles first.
 *
 * A profile that names the model the session is already on is marked as the
 * current one rather than repeated, and a discovered id that a profile already
 * covers is left out: two rows that do the same thing are a worse list than
 * one.
 */
export function modelOptions({
  profiles = null,
  defaultProfile = null,
  agentModels = null,
  discovered = null,
  current = null,
  vendor = null,
}: ModelPickerInput): ModelOption[] {
  const options: ModelOption[] = [];
  const covered = new Set<string>();

  for (const [name, profile] of Object.entries(profiles ?? {})) {
    const model = profile?.model ?? "";
    const provider = profile?.provider ?? "";
    if (model) covered.add(model);
    const agents = Object.entries(agentModels ?? {})
      .filter(([, assigned]) => assigned === name)
      .map(([agent]) => agent);
    const notes = [
      provider && model ? `${provider}/${model}` : provider || model,
      name === defaultProfile ? "default" : "",
      agents.length > 0 ? `used by ${agents.join(", ")}` : "",
    ].filter(Boolean);
    options.push({
      ref: name,
      label: name,
      detail: notes.join(" · "),
      origin: "profile",
      current: Boolean(current) && model === current,
    });
  }

  for (const model of discovered ?? []) {
    if (covered.has(model)) continue;
    covered.add(model);
    options.push({
      ref: model,
      label: model,
      detail: vendor ?? "",
      origin: "discovered",
      current: model === current,
    });
  }

  // The model in use is always offered, even when nothing lists it — a local
  // endpoint that refuses `GET /models` is the common case.
  if (current && !covered.has(current)) {
    options.unshift({
      ref: current,
      label: current,
      detail: [vendor, "in use"].filter(Boolean).join(" · "),
      origin: "current",
      current: true,
    });
  }

  return options;
}

/**
 * Where the session's model came from, when the daemon says.
 *
 * `session.list` may carry a source tag one day; until it does this reads
 * whatever is there and answers null rather than guessing, because a wrong
 * "pinned" is worse than no tag at all.
 */
export function modelSource(session: unknown): string | null {
  const value = session as { modelSource?: unknown; model_source?: unknown } | null;
  const raw = value?.modelSource ?? value?.model_source;
  return typeof raw === "string" && raw.length > 0 ? raw : null;
}
