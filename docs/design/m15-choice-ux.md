# M15b — Choice UX contract (questions, approvals, pickers) for TUI and desktop

Status: **binding**, 2026-09-14. Companion to `m15-agent-policies.md` §F.

## 1. One shape for every choice the daemon asks

Every choice goes through the question queue (`session/questions.py`) with the `ask_user`
shape already in protocol 1.5.0:

```
QuestionRequestParams { requestId, sessionId, timeoutSec?, questions: [
  { header: string,            // short chip, e.g. "Memory scope"
    question: string,          // full sentence, says why the answer matters
    options: [{ label, description?, preview? }],   // 2–8; the recommended one FIRST,
                                                     // its label ends with "(recommended)"
    multi?: boolean,           // Space toggles, Enter/Confirm submits
    allowOther?: boolean }     // the surface appends the "Other…" free-text row; the
] }                            // model never writes an "Other" option itself
QuestionRequestResult { answers: [{ selected: string[], text: string | null }] }
```

* Several **independent** questions go in one call and render as tabs (≤ 5); a question whose
  answer changes another is asked separately.
* `declined` (Esc / a surface that cannot ask) and `timed_out` stay distinct; neither is agreement.
* Options never appear in the question text — they render as pickable rows.

Core pickers that must use this shape (no bespoke prompts): `ask_user`, memory scope
(`memory_write`), `set_mode`, the tool-round checkpoint, `/skill create` and `/mcp add` when
run without arguments, and the setup wizard menus (`setup/ui.py`).

Approvals keep their four-way shape — **Allow once / Allow for this session / Allow for this
project / Deny** — plus **Deny with a reason** (free text that reaches the model as the
refusal), and Esc = deny once.

## 2. Keyboard map (identical on both surfaces)

| key | single-select | multi-select | batch of questions |
|---|---|---|---|
| ↑ ↓ (j k in the TUI) | move | move | move inside the active question |
| Space | — | toggle row | toggle row |
| Enter | pick and submit | confirm selection | pick / confirm; on the Confirm tab: submit all |
| 1–9 | jump to row N (no submit in multi) | toggle row N | same |
| ← → · Tab / Shift+Tab | — | — | previous / next question tab |
| Esc | decline / cancel | decline / cancel | decline the whole batch |
| Other… row + Enter | opens free text; Enter submits, Esc leaves the field | same | same |

Cursor starts on the recommended (first) option. A footer hint is always visible and names
exactly these keys. Batches show a marker per tab: ✓ answered · ▸ active · · pending, and a
Review/Confirm tab listing every answer with "(not answered)" in the warning colour.

## 3. Surfaces

* TUI: one `ChoiceList` primitive (`tui/src/components/ChoiceList.tsx`) used by QuestionPrompt,
  ApprovalPrompt, ConfirmMenu, ModelPicker, McpCatalogPicker, ToolChecklist, SkillCreateForm,
  McpAddForm; the setup wizard's `_menu_pick` (Python) implements the same map (adds ← →).
* Desktop: one `ChoiceList.vue` (`src/renderer/ui/`) used by QuestionModal, ApprovalModal,
  NewSkillSheet, McpServerSheet, the first-run wizard steps and any Select with ≤ 9 options.

## 4. Acceptance

* AC-M15b-1: an `ask_user` batch of three questions (one multi, one with previews) is answered
  end-to-end on both surfaces using only the keyboard map above.
* AC-M15b-2: `memory_write` without a scope shows "Project (name) (recommended) / Global / Cancel"
  on both surfaces; Esc declines and nothing is written.
* AC-M15b-3: an approval can be denied with a reason and the model's next turn quotes it.
* AC-M15b-4: `snowpea setup` menus accept ↑↓, Space, Enter, 1-9, ← → and Esc with the same hint line.
