/**
 * `/external new`: pick a CLI, a mode and optionally a model, then create.
 *
 * Steps are one small list or line each, so every key means what it means in
 * the other pickers: ↑↓ and Enter, Esc to go back (and out at the first step).
 * Choosing `unrestricted` ends with the typed confirmation.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import { mouseReports } from "../input/mouse.js";
import { currentAccent } from "../layout/palette.js";
import { fit } from "../layout/fit.js";
import { choiceHint, useChoiceKeys } from "../hooks/useChoiceKeys.js";
import type { ExternalCli, ExternalCliInfo, ExternalModeName } from "../rpc/sdk.js";
import {
  EXTERNAL_CLIS,
  cliLabel,
  modeLabel,
  modesFor,
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
  model: string | null;
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
}: {
  infos: ExternalCliInfo[];
  onChoose: (info: ExternalCliInfo) => void;
  onCancel: () => void;
  isActive: boolean;
  notice: string | null;
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
    </>
  );
}

function ModeStep({
  modes,
  onChoose,
  onBack,
  isActive,
}: {
  modes: ExternalModeName[];
  onChoose: (mode: ExternalModeName) => void;
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
      options={modes.map((mode) => ({
        label: modeLabel(mode),
        description: MODE_HINT[mode],
        danger: mode === "unrestricted",
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
function ModelStep({
  models,
  onSubmit,
  onBack,
  isActive,
  width,
}: {
  models: string[];
  onSubmit: (model: string | null) => void;
  onBack: () => void;
  isActive: boolean;
  width: number;
}): React.ReactElement {
  const [typing, setTyping] = useState(false);
  if (models.length > 0 && !typing) {
    return (
      <ConfirmMenu<ModelChoice>
        options={[
          { label: "The CLI's default model", value: null },
          ...models.map((model) => ({ label: fit(model, Math.max(10, width - 12)), value: model as ModelChoice })),
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
  return <ModelText onSubmit={onSubmit} onBack={models.length > 0 ? () => setTyping(false) : onBack} isActive={isActive} width={width} />;
}

function ModelText({
  onSubmit,
  onBack,
  isActive,
  width,
}: {
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
      <Text dimColor wrap="truncate-end">Enter create (empty = the CLI's default) · Esc back</Text>
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
  const [mode, setMode] = useState<ExternalModeName>("default");
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
            isActive={isActive}
            onCancel={onCancel}
            onChoose={(choice) => {
              if (!usable(choice)) {
                setNotice(`${cliLabel(choice.id)} is ${unavailableReason(choice) || "unavailable"}`);
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
          modes={modesFor(liveInfo ?? info)}
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
          width={width}
          isActive={isActive}
          onBack={() => setStep("mode")}
          onSubmit={(entered) => {
            setModel(entered);
            if (mode === "unrestricted") setStep("confirm");
            else onCreate({ cli: info.id, mode, model: entered });
          }}
        />
      ) : null}
      {step === "confirm" && info ? (
        <UnrestrictedConfirm
          width={width - 4}
          lines={unrestrictedPrompt({ cli: info.id, cwd: folder })}
          isActive={isActive}
          onCancel={() => setStep("model")}
          onConfirm={() => onCreate({ cli: info.id, mode: "unrestricted", model })}
        />
      ) : null}
    </Box>
  );
}
