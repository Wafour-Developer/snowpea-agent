/**
 * `/external new`: pick a CLI, a mode and optionally a model, then create.
 *
 * Steps are one small list or line each, so every key means what it means in
 * the other pickers: ↑↓ and Enter, Esc to go back (and out at the first step).
 * Choosing `unrestricted` ends with the typed confirmation.
 *
 * With a 1.56.0 daemon the mode step lists the CLI's own modes (`nativeModes`:
 * label plus a line built from what each asks and writes); a mode only a
 * person may choose ends with the typed confirmation too. An older daemon
 * keeps the three buckets.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import { mouseReports } from "../input/mouse.js";
import { currentAccent } from "../layout/palette.js";
import { fit } from "../layout/fit.js";
import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { ExternalCli, ExternalCliInfo, ExternalModeName, ExternalNativeMode } from "../rpc/sdk.js";
import {
  EXTERNAL_CLIS,
  cliLabel,
  modeLabel,
  modesFor,
  modelsNote,
  nativeModeDescription,
  nativeModesFor,
  pendingPathLine,
  pendingPathLines,
  personOnlyPrompt,
  personOnlyWord,
  unavailableReason,
  unrestrictedPrompt,
  usable,
} from "../state/external.js";
import { ChoiceList } from "./ChoiceList.js";
import { ConfirmMenu } from "./ConfirmMenu.js";
import { UnrestrictedConfirm } from "./UnrestrictedConfirm.js";

export interface NewExternalChoice {
  cli: ExternalCli;
  mode: ExternalModeName;
  /** The CLI's own mode (1.56.0); sent as `nativeMode`, which wins over `mode`. */
  nativeMode?: string;
  model: string | null;
}

/** One row of the mode step: a bucket (older daemon) or one of the CLI's own modes. */
type ModeRow = { mode: ExternalModeName; native: ExternalNativeMode | null };

function modeRows(info: ExternalCliInfo): ModeRow[] {
  const natives = nativeModesFor(info);
  if (natives.length > 0) return natives.map((native) => ({ mode: native.legacy, native }));
  return modesFor(info).map((mode) => ({ mode, native: null }));
}

/** A row that ends with the typed confirmation. */
function confirmed(row: ModeRow): boolean {
  return row.native ? row.native.personOnly : row.mode === "unrestricted";
}

function choiceOf(cli: ExternalCli, row: ModeRow, model: string | null): NewExternalChoice {
  return row.native
    ? { cli, mode: row.native.legacy, nativeMode: row.native.id, model }
    : { cli, mode: row.mode, model };
}

/** Rows the form occupies at most. */
export const EXTERNAL_NEW_ROWS = 14;

const MODE_HINT: Record<ExternalModeName, string> = {
  default: "snowpea asks before it runs things",
  readonly: "may read, not change anything",
  unrestricted: "no approvals; snowpea does not control what it runs",
};

type Step = "cli" | "mode" | "model" | "confirm";

function CliStep({
  infos,
  onChoose,
  onCancel,
  isActive,
  notice,
  pending,
}: {
  infos: ExternalCliInfo[];
  onChoose: (info: ExternalCliInfo) => void;
  onCancel: () => void;
  isActive: boolean;
  notice: string | null;
  /** Wrapped lines for the CLIs whose path is not confirmed. */
  pending: string[];
}): React.ReactElement {
  const [index, setIndex] = useState(() => Math.max(0, infos.findIndex(usable)));
  useChoiceKeys({
    count: infos.length,
    index,
    onIndex: setIndex,
    onEnter: (at) => infos[at] && onChoose(infos[at]),
    onCancel,
    isActive,
  });
  return (
    <>
      <ChoiceList
        options={infos.map((info) => ({
          label: `${cliLabel(info.id)}${info.version ? ` ${info.version}` : ""}`,
          description: usable(info) ? undefined : unavailableReason(info),
          badge: usable(info) ? undefined : "unavailable",
          badgeColor: "red",
        }))}
        selectedIndex={index}
        color={currentAccent()}
        descriptionMode="inline"
        hint={choiceHint({ enter: "pick" })}
      />
      {notice ? <Text color="red" wrap="truncate-end">{notice}</Text> : null}
      {pending.map((line, at) => (
        <Text key={`pending-${at}`} color="yellow" wrap="truncate-end">{line}</Text>
      ))}
    </>
  );
}

function ModeStep({
  modes,
  onChoose,
  onBack,
  isActive,
}: {
  modes: ModeRow[];
  onChoose: (mode: ModeRow) => void;
  onBack: () => void;
  isActive: boolean;
}): React.ReactElement {
  const [index, setIndex] = useState(0);
  useChoiceKeys({
    count: modes.length,
    index,
    onIndex: setIndex,
    onEnter: (at) => onChoose(modes[at]),
    onCancel: onBack,
    isActive,
  });
  return (
    <ChoiceList
      options={modes.map((row) => ({
        label: row.native ? row.native.label : modeLabel(row.mode),
        description: row.native ? nativeModeDescription(row.native) : MODE_HINT[row.mode],
        danger: confirmed(row),
      }))}
      selectedIndex={index}
      color={currentAccent()}
      descriptionMode="inline"
      hint={choiceHint({ enter: "pick", cancel: "back" })}
    />
  );
}

/** Marks the "type a name" row of the model list. */
const TYPE_A_NAME = Symbol("type-a-name");
const GO_BACK = Symbol("go-back");
type ModelChoice = string | null | typeof TYPE_A_NAME | typeof GO_BACK;

/** Rows of the model list shown at once. */
const MODEL_WINDOW = 7;

/**
 * The model: pick from what the CLI lists, or type a name. While the list is
 * empty (the CLI has not answered yet, or lists none) it is a text field.
 */
export function ModelStep({
  models,
  note,
  onSubmit,
  onBack,
  isActive,
  width,
  current,
  enterHint = "Enter create (empty = the CLI's default) · Esc back",
}: {
  models: string[];
  note: string | null;
  onSubmit: (model: string | null) => void;
  onBack: () => void;
  isActive: boolean;
  width: number;
  /** The model in use, marked in the list (`/model` in an open session). */
  current?: string | null;
  enterHint?: string;
}): React.ReactElement {
  const [typing, setTyping] = useState(false);
  if (models.length > 0 && !typing) {
    return (
      <ConfirmMenu<ModelChoice>
        options={[
          { label: `The CLI's default model${current === null ? " ✓" : ""}`, value: null },
          ...models.map((model) => ({
            label: fit(`${model}${model === current ? " ✓" : ""}`, Math.max(10, width - 12)),
            value: model as ModelChoice,
          })),
          { label: "Type a name…", value: TYPE_A_NAME },
        ]}
        escapeValue={GO_BACK}
        windowSize={MODEL_WINDOW}
        isActive={isActive}
        onChoose={(choice) => {
          if (choice === GO_BACK) onBack();
          else if (choice === TYPE_A_NAME) setTyping(true);
          else onSubmit(choice);
        }}
      />
    );
  }
  return (
    <ModelText
      onSubmit={onSubmit}
      onBack={models.length > 0 ? () => setTyping(false) : onBack}
      isActive={isActive}
      width={width}
      note={note}
      enterHint={enterHint}
    />
  );
}

function ModelText({
  onSubmit,
  onBack,
  isActive,
  width,
  note,
  enterHint,
}: {
  enterHint: string;
  /** "loading models…" while the CLI has not listed any, "no model list" once it said it lists none. */
  note: string | null;
  onSubmit: (model: string | null) => void;
  onBack: () => void;
  isActive: boolean;
  width: number;
}): React.ReactElement {
  const [text, setText] = useState("");
  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (key.escape) return onBack();
      if (key.return) return onSubmit(text.trim() || null);
      if (key.backspace || key.delete) return setText((value) => value.slice(0, -1));
      if (input && !key.ctrl && !key.meta && !key.tab) setText((value) => value + input);
    },
    { isActive },
  );
  return (
    <>
      <Box>
        <Text dimColor>model: </Text>
        <Text>{fit(text, Math.max(1, width - 16))}</Text>
        <Text inverse> </Text>
      </Box>
      {note ? <Text dimColor wrap="truncate-end">{note}</Text> : null}
      <Text dimColor wrap="truncate-end">{enterHint}</Text>
    </>
  );
}

export function ExternalNewForm({
  infos,
  folder,
  width,
  onCreate,
  onCancel,
  isActive = true,
}: {
  /** `external.clis`, any order; shown in the fixed CLI order. */
  infos: ExternalCliInfo[];
  folder: string;
  width: number;
  onCreate: (choice: NewExternalChoice) => void;
  onCancel: () => void;
  isActive?: boolean;
}): React.ReactElement {
  const ordered = EXTERNAL_CLIS.map((id) => infos.find((info) => info.id === id)).filter(
    (info): info is ExternalCliInfo => info !== undefined,
  );
  const [step, setStep] = useState<Step>("cli");
  const [info, setInfo] = useState<ExternalCliInfo | null>(null);
  const [mode, setMode] = useState<ModeRow>({ mode: "default", native: null });
  const [model, setModel] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // `external.clis.changed` brings the model list in while the form is open.
  const liveInfo = info ? (infos.find((entry) => entry.id === info.id) ?? info) : null;

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="blue" paddingX={1}>
      <Text bold color="blue" wrap="truncate-end">
        {fit(`New external session · ${folder}`, width - 4)}
      </Text>
      {step === "cli" ? (
        ordered.length === 0 ? (
          <Text color="red" wrap="truncate-end">no external CLI is known to this daemon</Text>
        ) : (
          <CliStep
            infos={ordered}
            notice={notice}
            pending={pendingPathLines(ordered, width - 4)}
            isActive={isActive}
            onCancel={onCancel}
            onChoose={(choice) => {
              if (!usable(choice)) {
                setNotice(
                  choice.pendingPath
                    ? (pendingPathLine(choice) ?? "")
                    : `${cliLabel(choice.id)} is ${unavailableReason(choice) || "unavailable"}`,
                );
                return;
              }
              setNotice(null);
              setInfo(choice);
              setStep("mode");
            }}
          />
        )
      ) : null}
      {step === "mode" && info ? (
        <ModeStep
          modes={modeRows(liveInfo ?? info)}
          isActive={isActive}
          onBack={() => setStep("cli")}
          onChoose={(chosen) => {
            setMode(chosen);
            setStep("model");
          }}
        />
      ) : null}
      {step === "model" && info ? (
        <ModelStep
          models={liveInfo?.models ?? []}
          note={modelsNote(liveInfo)}
          width={width}
          isActive={isActive}
          onBack={() => setStep("mode")}
          onSubmit={(entered) => {
            setModel(entered);
            if (confirmed(mode)) setStep("confirm");
            else onCreate(choiceOf(info.id, mode, entered));
          }}
        />
      ) : null}
      {step === "confirm" && info ? (
        <UnrestrictedConfirm
          width={width - 4}
          lines={
            mode.native
              ? personOnlyPrompt({ cli: info.id, cwd: folder, mode: mode.native })
              : unrestrictedPrompt({ cli: info.id, cwd: folder })
          }
          word={mode.native ? personOnlyWord(mode.native) : undefined}
          isActive={isActive}
          onCancel={() => setStep("model")}
          onConfirm={() => onCreate(choiceOf(info.id, mode, model))}
        />
      ) : null}
    </Box>
  );
}
