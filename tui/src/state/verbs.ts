/**
 * The word the working indicator shows while the agent is busy.
 *
 * It is there to make waiting feel like something is happening, so the list is
 * long enough that a verb rarely repeats inside one turn, and it leans on the
 * garden the project is named after. The Korean list is used when the user
 * wrote in Korean, so the wait reads in the language they are working in.
 */

/** How long one verb stays up before the next takes over. */
export const VERB_PERIOD_MS = 8000;

export const ENGLISH_VERBS: readonly string[] = [
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

export const KOREAN_VERBS: readonly string[] = [
  "생각 중",
  "궁리 중",
  "새싹 틔우는 중",
  "뒤적이는 중",
  "엮는 중",
  "헤아리는 중",
  "다듬는 중",
  "우려내는 중",
  "만지작거리는 중",
  "뒤지는 중",
  "짜맞추는 중",
  "발아 중",
  "여무는 중",
  "덩굴 뻗는 중",
  "꼬투리 채우는 중",
  "버무리는 중",
  "익히는 중",
  "고민 중",
  "계산 중",
  "살펴보는 중",
  "파고드는 중",
  "정리하는 중",
  "빚는 중",
  "깎는 중",
  "숙성 중",
  "캐는 중",
  "매만지는 중",
  "조립 중",
  "굴리는 중",
  "탐색 중",
  "저울질 중",
  "얽는 중",
  "채집 중",
  "발효 중",
  "눈여겨보는 중",
  "손보는 중",
  "훑는 중",
  "궁금해하는 중",
  "조율 중",
  "갈무리 중",
];

/** Hangul syllables, jamo included. */
const HANGUL = /[ᄀ-ᇿ㄰-㆏가-힯]/;

/** True when the text is written in Korean. */
export function isKorean(text: string | null | undefined): boolean {
  return typeof text === "string" && HANGUL.test(text);
}

/** The list to draw from for this prompt. */
export function verbsFor(prompt: string | null | undefined): readonly string[] {
  return isKorean(prompt) ? KOREAN_VERBS : ENGLISH_VERBS;
}

/**
 * The verb for a turn that has been running `elapsedMs`.
 *
 * `offset` moves the starting point so two turns in a row do not open with the
 * same word; the caller passes something cheap and stable, such as the number
 * of messages so far.
 */
export function verbAt(
  verbs: readonly string[],
  elapsedMs: number,
  offset = 0,
  periodMs: number = VERB_PERIOD_MS,
): string {
  if (verbs.length === 0) return "";
  const steps = Math.floor(Math.max(0, elapsedMs) / Math.max(1, periodMs));
  const index = (steps + Math.max(0, Math.floor(offset))) % verbs.length;
  return verbs[index];
}
