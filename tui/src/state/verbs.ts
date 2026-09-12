/**
 * The word the working indicator shows while the agent is busy.
 *
 * It is there to make waiting feel like something is happening, so the list is
 * long enough that a verb rarely repeats inside one turn, and it leans on the
 * garden the project is named after. English only, whatever language the user
 * writes in: the verb is the product's voice, not a translation of the prompt.
 */

/** How long one verb stays up before the next takes over. */
export const VERB_PERIOD_MS = 8000;

export const VERBS: readonly string[] = [
  "Scurrying",
  "Pondering",
  "Brewing",
  "Noodling",
  "Sprouting",
  "Germinating",
  "Weaving",
  "Spelunking",
  "Puttering",
  "Tinkering",
  "Percolating",
  "Simmering",
  "Rummaging",
  "Untangling",
  "Conjuring",
  "Whittling",
  "Burrowing",
  "Foraging",
  "Composting",
  "Trellising",
  "Pollinating",
  "Mulching",
  "Sifting",
  "Kneading",
  "Marinating",
  "Distilling",
  "Wandering",
  "Scheming",
  "Doodling",
  "Shuffling",
  "Juggling",
  "Orbiting",
  "Hatching",
  "Blossoming",
  "Rooting",
  "Vining",
  "Podding",
  "Harvesting",
  "Reticulating",
  "Calibrating",
  "Meandering",
  "Fermenting",
  "Nesting",
  "Puzzling",
  "Wrangling",
  "Shelling",
];

/**
 * The verb for a turn that has been running `elapsedMs`.
 *
 * `offset` moves the starting point so two turns in a row do not open with the
 * same word; the caller passes something cheap and stable, such as the number
 * of messages so far.
 */
export function verbAt(
  elapsedMs: number,
  offset = 0,
  verbs: readonly string[] = VERBS,
  periodMs: number = VERB_PERIOD_MS,
): string {
  if (verbs.length === 0) return "";
  const steps = Math.floor(Math.max(0, elapsedMs) / Math.max(1, periodMs));
  const index = (steps + Math.max(0, Math.floor(offset))) % verbs.length;
  return verbs[index];
}
