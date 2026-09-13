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
export type ModelOrigin = "profile" | "discovered" | "current" | "inherit";

/** The ref that clears a session's pin and lets the routing decide again. */
export const INHERIT_REF = "inherit";

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
  /** `models.profiles` from the global `settings.get`. */
  profiles?: Record<string, ModelProfile> | null;
  /** `models.profiles` from the project document; these are tagged. */
  projectProfiles?: Record<string, ModelProfile> | null;
  /** `models.default`, marked in the listing. */
  defaultProfile?: string | null;
  /** `agents.models`: which profile each named agent uses. */
  agentModels?: Record<string, string> | null;
  /** `provider.models` — what the vendor's endpoint reports. */
  discovered?: string[] | null;
  /**
   * Which rung of the daemon's model chain answered: `live` (the vendor's own
   * endpoint), `settings`, `cache` or `curated`. Anything but `live` is a
   * fallback, and a picker that does not say so invites the user to blame the
   * model for an id the vendor never confirmed it serves.
   */
  discoveredSource?: string | null;
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
  projectProfiles = null,
  defaultProfile = null,
  agentModels = null,
  discovered = null,
  discoveredSource = null,
  current = null,
  vendor = null,
}: ModelPickerInput): ModelOption[] {
  const options: ModelOption[] = [];
  const covered = new Set<string>();
  const named = new Set<string>();

  // The project's profiles come first and shadow a global one of the same
  // name, which is the order the daemon resolves them in.
  const sources: Array<[Record<string, ModelProfile> | null, boolean]> = [
    [projectProfiles, true],
    [profiles, false],
  ];
  for (const [document, fromProject] of sources) {
    for (const [name, profile] of Object.entries(document ?? {})) {
      if (named.has(name)) continue;
      named.add(name);
      const model = profile?.model ?? "";
      const provider = profile?.provider ?? "";
      if (model) covered.add(model);
      const agents = Object.entries(agentModels ?? {})
        .filter(([, assigned]) => assigned === name)
        .map(([agent]) => agent);
      const notes = [
        fromProject ? "[project]" : "",
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
  }

  const fallbackTag =
    { settings: "from settings", cache: "cached list", curated: "curated list" }[
      discoveredSource ?? ""
    ] ?? "";
  for (const model of discovered ?? []) {
    if (covered.has(model)) continue;
    covered.add(model);
    options.push({
      ref: model,
      label: model,
      detail: [vendor, fallbackTag].filter(Boolean).join(" · "),
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

  // Clearing the pin is a choice like any other, so it is a row rather than a
  // command you have to know about.
  options.push({
    ref: INHERIT_REF,
    label: "inherit (clear pin)",
    detail: "let the project and global defaults decide again",
    origin: "inherit",
    current: false,
  });

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
