import { fitWordmark, letterWidthFor, renderWordmark, shadowRow, wordmarkWidth } from "../src/layout/wordmark.js";
for (const cols of [160, 120, 80, 60]) {
  const w = letterWidthFor(cols);
  const rows = w ? renderWordmark(w) : null;
  console.log(`--- ${cols} cols · letter ${w} · width ${rows ? [...rows[0]].length : 0} ---`);
  if (rows) { console.log(rows.join("\n")); console.log(shadowRow([...rows[0]].length)); }
  else console.log("(compact fallback)");
}
