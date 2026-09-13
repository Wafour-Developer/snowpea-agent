# Deviations — CORE-tui-polish (the help panel, section rules, footer order, the agent panel)

Five small TUI commits — `a108071`, `50af009`, `6a46da9`, `a061106`, `b80ed64` — that together make
the bottom of the screen readable: a dismissible help panel, dividers between the persistent regions,
the footer in the right order, and an agent panel that says what it is hiding. Recorded here per
`docs/design/deviations/README.md`.

1. **F1 is handled by a raw `stdin.on("data")` listener, not by `useInput`.** `tui/src/app.tsx:527-536`
   attaches its own data handler and matches F1's escape sequences directly. This looks like an
   accident and is not: Ink 5 removed F1 from `useInput` entirely, so there is no hook-shaped way to
   receive it. The listener reads *only* F1's sequences and leaves everything else to Ink, so the two
   input paths cannot fight over a keystroke. Anyone tidying this back into `useInput` will silently
   break the help key.

2. **`HelpPanel` reuses `wrapLine()` from `layout/transcript.ts` rather than wrapping its own text.**
   `components/HelpPanel.tsx:5,49,55`. Help text and transcript text then wrap identically — same
   grapheme handling, same continuation indent — which matters more than the module boundary being
   clean, because a help panel that wraps CJK differently from the conversation above it is exactly
   where a width bug gets noticed and misattributed.

3. **The help panel is height-bounded and scrollable, and suppressed rather than layered.** It takes
   `width`/`height`/`isActive`, keeps an `offset`, handles PgUp/PgDn and the arrows, and shows a
   `start+1-end/total` footer. It closes on Esc, Enter, `q` or F1, and is suppressed entirely during
   approvals, an update confirmation, or a running turn — a modal that can cover an approval prompt
   is a way to lose an approval. `Chat.tsx` lost its `onToggleHelp` prop in the process.

4. **`layout.statusRows` adds a hard-coded `+ 3` for the three section rules.** `app.tsx:864-869`
   sums the HUD rows, the context warning, the summary line, the agent rows, and then a literal `3`.
   It is a magic number and it will drift the moment a fourth rule is added or one is removed; it was
   taken over deriving the count from the rendered tree because the layout has to be computed before
   the tree exists. Adding or removing a `SectionRule` means changing this number in the same commit.

5. **The agent panel's current row is hard-coded `"main"`; the team name moved to a rule label.**
   `a061106` stopped defaulting `activeTeam` to `"main"` — the session was being labelled with the
   team's name, which read as though the session *was* the team. The current row now always says
   `main` (the session's own agent, `app.tsx:807`) and the team appears, only when one is genuinely
   active, as `Team: <name>` rendered into the divider itself
   (`app.tsx:1548`, `components/SectionRule.tsx:5-21`). The label is right-aligned into the rule and
   truncated to the available width, so it can never push the layout.

6. **The footer reorder (`6a46da9`) is a pure JSX move with no test.** `StatusHud` renders after the
   mode/summary line. There is nothing to assert that a snapshot would not have to be rewritten by
   hand; only `tui/dist` was regenerated. Recorded so the ordering is understood as deliberate rather
   than incidental to some other change.

7. **The collapsed agent row counts what is hidden, not the total.** `layout/agents.ts:168-180` now
   reads `"N more idle agents"` and lists the hidden names in the row's `task` field. The previous
   wording gave the total, which double-counted the rows already visible above it.
