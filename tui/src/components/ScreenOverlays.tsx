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
import { ApprovalPrompt, approvalExtraRows } from "./ApprovalPrompt.js";
import { ConfirmMenu } from "./ConfirmMenu.js";
import { DelegationConfirm, delegationConfirmRows } from "./DelegationConfirm.js";
import { EXTERNAL_NEW_ROWS, ExternalNewForm } from "./ExternalNewForm.js";
import { EXTERNAL_PICKER_TOTAL_ROWS, ExternalPicker } from "./ExternalPicker.js";
import { QuestionPrompt, questionPromptRows } from "./QuestionPrompt.js";
import { UnrestrictedConfirm, unrestrictedConfirmRows } from "./UnrestrictedConfirm.js";
import { rowStatus, sessionLabel, parseProposal } from "../state/jarvis.js";
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
    case "answer": {
      const item = screens.waitingItem;
      if (!item) return 0;
      if (item.kind === "question") return questionPromptRows(item.request as never, width) + 1;
      const request = item.request as ApprovalEntry;
      const proposal = parseProposal(request);
      if (proposal) return delegationConfirmRows(proposal);
      return approvalPromptRows(Object.keys(request.args ?? {}).length) + approvalExtraRows(request);
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
    const inner = Math.max(20, width - 8);
    return (
      <ConfirmMenu<number | null>
        options={[
          ...screens.jarvis.rows.slice(0, 20).map((row, index) => ({
            label: spread(`${index + 1}. ${sessionLabel(row)}`, rowStatus(row, now).text, inner),
            value: index as number | null,
          })),
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
        width={width}
        onConfirm={screens.confirmUnrestricted}
        onCancel={() => screens.setUi(null)}
      />
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
  const proposal = parseProposal(request);
  if (proposal) {
    return (
      <DelegationConfirm
        proposal={proposal}
        label={item.label}
        width={width}
        now={now}
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
      mode="accept"
      onDecide={(answer, reason) => screens.answerApproval(item.requestId, answer.decision, answer.scope, reason)}
    />
  );
}
