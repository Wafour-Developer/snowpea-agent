/**
 * The pickers and prompts of the Jarvis and external screens, drawn in place of
 * the input line: the external-session picker, the new-session form, the list
 * of supervised sessions, what waits for the person (any session, session name
 * shown) and the typed confirmation for unrestricted mode.
 */

import React from "react";

import type { JarvisScreens } from "../hooks/useJarvisScreens.js";
import { approvalPromptRows } from "../layout/viewport.js";
import { fit } from "../layout/fit.js";
import { ApprovalPrompt } from "./ApprovalPrompt.js";
import { ConfirmMenu } from "./ConfirmMenu.js";
import { DelegationConfirm, delegationConfirmRows } from "./DelegationConfirm.js";
import { EXTERNAL_NEW_ROWS, ExternalNewForm } from "./ExternalNewForm.js";
import { EXTERNAL_PICKER_TOTAL_ROWS, ExternalPicker } from "./ExternalPicker.js";
import { QuestionPrompt, questionPromptRows } from "./QuestionPrompt.js";
import { UnrestrictedConfirm, unrestrictedConfirmRows } from "./UnrestrictedConfirm.js";
import { fitChips, personOnlyView, rowAgent, rowChips, rowStatus, sessionName } from "../state/jarvis.js";
import { tagCell, tagColumnWidth } from "../state/agent-tag.js";
import { spread } from "../layout/fit.js";
import type { ApprovalEntry } from "../state/store.js";

/** Rows the open overlay needs, so the full-screen layout reserves them. */
export function screenOverlayRows(screens: JarvisScreens, width: number): number {
  const ui = screens.ui;
  if (!ui) return 0;
  switch (ui.kind) {
    case "picker":
      return EXTERNAL_PICKER_TOTAL_ROWS;
    case "new":
      return EXTERNAL_NEW_ROWS;
    case "sessions":
      return Math.min(screens.jarvis.rows.length, 10) + 4;
    case "unrestricted":
      return unrestrictedConfirmRows(ui.lines.length);
    case "adopt": {
      const view = personOnlyView(ui.request, 0);
      return view ? delegationConfirmRows(view) : 0;
    }
    case "confirm":
      return delegationConfirmRows(ui.view);
    case "answer": {
      const item = screens.waitingItem;
      if (!item) return 0;
      if (item.kind === "question") return questionPromptRows(item.request as never, width) + 1;
      const request = item.request as ApprovalEntry;
      const view = personOnlyView(request, 0);
      if (view) return delegationConfirmRows(view);
      return approvalPromptRows(Object.keys(request.args ?? {}).length);
    }
  }
}

export function ScreenOverlays({
  screens,
  width,
  folder,
  now,
}: {
  screens: JarvisScreens;
  width: number;
  folder: string;
  now: number;
}): React.ReactElement | null {
  const ui = screens.ui;
  if (!ui) return null;

  if (ui.kind === "picker") {
    return (
      <ExternalPicker
        rows={ui.rows}
        folder={folder}
        allProjects={ui.allProjects}
        loading={ui.loading}
        hasMore={ui.nextBefore !== null}
        error={ui.error}
        unavailable={ui.unavailable}
        pending={ui.pending}
        width={width}
        onOpen={(row) => screens.openExternalRef(row.ref)}
        onNew={screens.openNewForm}
        onToggleAll={screens.pickerToggleAll}
        onMore={screens.pickerMore}
        onCancel={() => screens.setUi(null)}
      />
    );
  }

  if (ui.kind === "new") {
    return (
      <ExternalNewForm
        infos={ui.infos}
        folder={folder}
        width={width}
        onCancel={() => screens.setUi(null)}
        onCreate={screens.createExternal}
      />
    );
  }

  if (ui.kind === "sessions") {
    const shown = screens.jarvis.rows.slice(0, 20);
    const agents = shown.map(rowAgent);
    const column = tagColumnWidth(agents);
    // The tag, the status and the colours are the panel's: a tag in its own
    // colour, a session snowpea does not gate in red unless it waits on you.
    const inner = Math.max(20, width - 8 - column);
    return (
      <ConfirmMenu<number | null>
        options={[
          ...shown.map((row, index) => {
            const status = rowStatus(row, now);
            const color = row.controlled === false && status.color !== "yellow" ? "red" : status.color;
            const chips = rowChips(row, width);
            const right = fitChips([status.text, ...chips], Math.max(12, Math.floor(inner / 2)));
            return {
              label: spread(`${String(index + 1).padStart(2)}. ${sessionName(row)}`, right, inner),
              value: index as number | null,
              tag: column > 0 ? tagCell(agents[index], column) : undefined,
              tagColor: agents[index].color,
              color,
            };
          }),
          { label: "Cancel", value: null },
        ]}
        escapeValue={null}
        windowSize={10}
        onChoose={(index) => {
          if (index === null) screens.setUi(null);
          else screens.openRow(screens.jarvis.rows[index]);
        }}
      />
    );
  }

  if (ui.kind === "unrestricted") {
    return (
      <UnrestrictedConfirm
        lines={ui.lines}
        word={ui.word}
        width={width}
        onConfirm={screens.confirmUnrestricted}
        onCancel={() => screens.setUi(null)}
      />
    );
  }

  if (ui.kind === "confirm") {
    return (
      <DelegationConfirm
        view={ui.view}
        label={ui.label}
        width={width}
        onAnswer={(confirmed) => (confirmed ? ui.onYes() : ui.onNo())}
      />
    );
  }

  if (ui.kind === "adopt") {
    const view = personOnlyView(ui.request, now);
    if (!view) return null;
    return (
      <DelegationConfirm view={view} label={view.from} width={width} onAnswer={screens.answerAdopt} />
    );
  }

  const item = screens.waitingItem;
  if (!item) return null;
  if (item.kind === "question") {
    return (
      <QuestionPrompt
        request={item.request as never}
        label={fit(item.label, Math.max(10, width - 12))}
        onAnswer={(answers) => screens.answerQuestion(item.requestId, answers)}
      />
    );
  }
  const request: ApprovalEntry = { ...(item.request as ApprovalEntry), source: "queue" };
  const view = personOnlyView(request, now);
  if (view) {
    return (
      <DelegationConfirm
        view={view}
        label={item.label}
        width={width}
        onAnswer={(confirmed) =>
          screens.answerApproval(
            item.requestId,
            confirmed ? "allow" : "deny",
            "once",
            confirmed ? undefined : "declined by the person",
          )
        }
      />
    );
  }
  return (
    <ApprovalPrompt
      request={request}
      label={fit(item.label, Math.max(10, width - 24))}
      crossSession
      onceOnly={request.personOnly === true}
      mode="accept"
      onDecide={(answer, reason) => screens.answerApproval(item.requestId, answer.decision, answer.scope, reason)}
    />
  );
}
