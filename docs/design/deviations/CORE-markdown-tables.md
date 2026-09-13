# Deviations — CORE-markdown-tables (terminal-cell widths and Markdown tables in the transcript)

The table half of `f55d57f`. A model answering in Markdown emits tables constantly, and the TUI was
printing the raw pipe-delimited source; worse, any line containing CJK text or emoji wrapped in the
wrong place, because width was being measured in JavaScript code units. Recorded here per
`docs/design/deviations/README.md`.

1. **Cell width is measured with `Intl.Segmenter` graphemes and a hand-written wide-character rule,
   not a `wcwidth` dependency.** `tui/src/layout/text-width.ts` segments at grapheme boundaries, gives
   combining marks and control/format characters width 0, emoji-presentation and regional-indicator
   sequences width 2, and applies an explicit East Asian wide/fullwidth code-point range for the rest.
   `Intl.Segmenter` is in the runtime already; a `wcwidth` package would have added a dependency to
   the TUI bundle to answer a question the platform can answer. Ambiguous-width characters
   deliberately stay at one cell — guessing 2 breaks far more Latin-adjacent text than it fixes.

2. **`MessageStream.tsx` was gutted so inline and full-screen rendering share one code path.** It is
   30 lines at HEAD and does nothing but call `messageLines()` and `wrapLine()` from
   `layout/transcript.ts`. Deleting a parallel renderer was the point of the change, not a side
   effect: two renderers meant every width or Markdown fix had to be made twice, and the full-screen
   one was already a version behind.

3. **A table that cannot fit falls back to stacked `key: value` rows.** `tableAt()`
   (`layout/transcript.ts:162-…`) measures the columns and, when there are too many for the terminal,
   emits each row as stacked label/value lines instead. Truncating columns loses data silently and
   horizontal scrolling does not exist in a scrollback transcript, so the fallback preserves every
   value at the cost of vertical space.

4. **Tables inside code fences are left literal.** `tableCells()`/`tableAt()` are fence-aware. A
   pipe-delimited block inside a fence is usually *about* Markdown — documentation, a diff, a test
   fixture — and rendering it as a box-drawn table would destroy the thing being shown.

5. **The header/divider pair is required before anything is treated as a table.** `tableAt()` parses
   `source[start]` and `source[start + 1]` and bails unless both are cell rows. Prose containing a
   stray `|` is common; a false positive would silently reflow a paragraph into columns.
