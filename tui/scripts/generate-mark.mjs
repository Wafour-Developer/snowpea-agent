/**
 * Rasterise the snowpea mark into the half-block bitmaps the TUI draws.
 *
 * The mark is a monoline drawing — a stem, a leaf, a ring and a dot, all one
 * stroke weight with round caps — which is what makes this exact rather than
 * approximate: a round-capped stroke is every point within half the stroke
 * width of the path, so coverage is a distance test and no path rasteriser is
 * needed. Béziers are flattened; the circles are true circles.
 *
 * It runs here, not at startup: the output is checked in as
 * `tui/src/components/mark.ts`, so the TUI parses no SVG and ships no
 * geometry code.
 *
 *   node tui/scripts/generate-mark.mjs [path/to/mark.svg]
 *
 * Re-run it when the brand mark changes; the header of the generated file
 * records which source it came from.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_SVG = resolve(
  HERE,
  "../../../snowpea-site/shared/brand/mark.svg",
);
const OUT = join(HERE, "../src/components/mark.ts");

/**
 * The marks the TUI draws.
 *
 * The big one is square: the letters are seven rows tall, a terminal cell is
 * about twice as tall as it is wide, so fourteen half-pixel rows want about
 * sixteen columns beside them.
 *
 * The small one is the ring alone. A two-row mark has four half-pixels of
 * height to work with, and the whole sprout shaken down into that is mush —
 * so it takes the reduction the brand itself makes, where the favicon becomes
 * dot, arc and ring.
 */
const SIZES = [
  { name: "BIG_MARK", width: 16, height: 14, only: "all" },
  { name: "MINI_MARK", width: 6, height: 4, only: "ring" },
];

/** Samples per half-pixel, per axis; 4×4 is enough to keep the ring open. */
const SUPERSAMPLE = 4;
/** Ink when this much of the cell is covered. */
const THRESHOLD = 0.25;

// --- reading the mark ------------------------------------------------------

/** Numbers out of an SVG attribute or path chunk. */
function numbers(text) {
  return (text.match(/-?\d*\.?\d+(?:e-?\d+)?/gi) ?? []).map(Number);
}

/**
 * Flatten one `d` attribute into a polyline.
 *
 * The mark uses only `M`, `C` and `Z`, which is all this handles: a general
 * path parser would be more code than the drawing it reads.
 */
function flattenPath(d, steps = 64) {
  const points = [];
  let cursor = [0, 0];
  let start = [0, 0];
  const tokens = d.match(/[MCZmcz][^MCZmcz]*/g) ?? [];
  for (const token of tokens) {
    const command = token[0];
    const args = numbers(token.slice(1));
    if (command === "M" || command === "m") {
      cursor = command === "M" ? [args[0], args[1]] : [cursor[0] + args[0], cursor[1] + args[1]];
      start = cursor;
      points.push(cursor);
      // Extra coordinate pairs after a moveto are implicit linetos.
      for (let i = 2; i + 1 < args.length; i += 2) {
        cursor = command === "M" ? [args[i], args[i + 1]] : [cursor[0] + args[i], cursor[1] + args[i + 1]];
        points.push(cursor);
      }
      continue;
    }
    if (command === "Z" || command === "z") {
      points.push(start);
      cursor = start;
      continue;
    }
    // Cubic Béziers, one or more sets of three points.
    for (let i = 0; i + 5 < args.length; i += 6) {
      const [x1, y1, x2, y2, x, y] =
        command === "C"
          ? args.slice(i, i + 6)
          : [
              cursor[0] + args[i],
              cursor[1] + args[i + 1],
              cursor[0] + args[i + 2],
              cursor[1] + args[i + 3],
              cursor[0] + args[i + 4],
              cursor[1] + args[i + 5],
            ];
      const [x0, y0] = cursor;
      for (let step = 1; step <= steps; step += 1) {
        const t = step / steps;
        const u = 1 - t;
        points.push([
          u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x,
          u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y,
        ]);
      }
      cursor = [x, y];
    }
  }
  return points;
}

/** The primitives the mark is made of, read out of the SVG. */
function readMark(svg) {
  const viewBox = numbers(/viewBox="([^"]+)"/.exec(svg)?.[1] ?? "0 0 32 32");
  const strokeWidth = Number(/stroke-width="([\d.]+)"/.exec(svg)?.[1] ?? 2.8);
  const polylines = [...svg.matchAll(/<path[^>]*\sd="([^"]+)"/g)].map((match) =>
    flattenPath(match[1]),
  );
  const circles = [...svg.matchAll(/<circle[^>]*>/g)].map((match) => {
    const element = match[0];
    return {
      cx: Number(/cx="([\d.]+)"/.exec(element)?.[1]),
      cy: Number(/cy="([\d.]+)"/.exec(element)?.[1]),
      r: Number(/r="([\d.]+)"/.exec(element)?.[1]),
      filled: /fill="(?!none)/.test(element),
    };
  });
  return { viewBox, strokeWidth, polylines, circles };
}

// --- sampling --------------------------------------------------------------

function distanceToSegment(px, py, [ax, ay], [bx, by]) {
  const dx = bx - ax;
  const dy = by - ay;
  const lengthSquared = dx * dx + dy * dy;
  const t = lengthSquared === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSquared));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/** True when the point is inside the drawn mark. */
function covered({ polylines, circles, strokeWidth }, x, y) {
  const half = strokeWidth / 2;
  for (const points of polylines) {
    for (let i = 1; i < points.length; i += 1) {
      if (distanceToSegment(x, y, points[i - 1], points[i]) <= half) return true;
    }
  }
  for (const circle of circles) {
    const distance = Math.hypot(x - circle.cx, y - circle.cy);
    if (circle.filled) {
      if (distance <= circle.r) return true;
      continue;
    }
    // A stroked circle is a ring: near the circumference, not inside it.
    if (Math.abs(distance - circle.r) <= half) return true;
  }
  return false;
}

/** The part of the mark a size draws, and the box to fit it in. */
function subject(mark, only) {
  if (only !== "ring") return { ...mark, box: mark.viewBox };
  const ring = mark.circles.find((circle) => !circle.filled);
  const half = mark.strokeWidth / 2;
  const reach = ring.r + half;
  return {
    ...mark,
    polylines: [],
    circles: [ring],
    box: [ring.cx - reach, ring.cy - reach, reach * 2, reach * 2],
  };
}

/** One bitmap: `height` rows of `width` characters, `#` where the ink is. */
function rasterise(mark, width, height) {
  const [minX, minY, boxWidth, boxHeight] = mark.box ?? mark.viewBox;
  const rows = [];
  for (let row = 0; row < height; row += 1) {
    let line = "";
    for (let column = 0; column < width; column += 1) {
      let hits = 0;
      for (let sy = 0; sy < SUPERSAMPLE; sy += 1) {
        for (let sx = 0; sx < SUPERSAMPLE; sx += 1) {
          const u = (column + (sx + 0.5) / SUPERSAMPLE) / width;
          const v = (row + (sy + 0.5) / SUPERSAMPLE) / height;
          if (covered(mark, minX + u * boxWidth, minY + v * boxHeight)) hits += 1;
        }
      }
      line += hits / (SUPERSAMPLE * SUPERSAMPLE) >= THRESHOLD ? "#" : ".";
    }
    rows.push(line);
  }
  return rows;
}

// --- writing it out --------------------------------------------------------

const source = process.argv[2] ?? DEFAULT_SVG;
const svg = readFileSync(source, "utf8");
const mark = readMark(svg);

const bitmaps = SIZES.map(({ name, width, height, only }) => {
  const rows = rasterise(subject(mark, only), width, height);
  const body = rows.map((row) => `  "${row}",`).join("\n");
  return `/** ${width} columns × ${height} half-pixels, ${height / 2} terminal rows. */\nexport const ${name}: readonly string[] = [\n${body}\n];`;
});

const file = `/**
 * The snowpea mark, as half-block bitmaps.
 *
 * Generated from the brand SVG by \`tui/scripts/generate-mark.mjs\` — do not
 * edit by hand; re-run the script when the mark changes. Each row is one
 * half-pixel line, two of them to a terminal row, exactly like the letters in
 * \`layout/wordmark.ts\`.
 *
 * Source: ${source.replace(process.cwd() + "/", "")}
 * Shape: a monoline sprout — a stem on the diagonal, one leaf, an open ring
 * for the pea and WAFOUR's counter dot as a second pea.
 */

${bitmaps.join("\n\n")}
`;

writeFileSync(OUT, file, "utf8");
for (const { name, width, height, only } of SIZES) {
  console.log(`${name}: ${width}×${height}`);
  for (const row of rasterise(subject(mark, only), width, height)) {
    console.log(`  ${row.replace(/#/g, "█").replace(/\./g, " ")}`);
  }
}
console.log(`\nwrote ${OUT}`);
