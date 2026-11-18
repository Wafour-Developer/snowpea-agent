/**
 * `/providers`: connected providers, one provider's models, and the question
 * before a provider is disconnected (capability `hiddenModels`, 1.60.0).
 *
 * Enter on a model removes it from the list (hidden, or taken off the
 * hand-added list — the transcript says which); Enter on a hidden model shows
 * it again; Disconnect asks first. Esc goes back one stage. A hidden model
 * that something still uses is shown with a warning: it keeps working.
 */

import React from "react";
import { Box, Text } from "ink";

import type { ProvidersUi } from "../hooks/useProviders.js";
import { say } from "../state/errors.js";
import {
  disconnectConfirmText,
  hiddenInUseLines,
  providerBadges,
  providerMenu,
  type ProviderAction,
} from "../state/providers.js";
import { sanitizeText } from "../util/sanitize.js";
import { ConfirmMenu, type ConfirmOption } from "./ConfirmMenu.js";

/** Rows of a menu drawn at once. */
export const PROVIDERS_ROWS = 10;

export interface ProvidersScreenProps {
  ui: ProvidersUi;
  /** The daemon has `hiddenModels`: model rows are offered. */
  supported: boolean;
  width: number;
  isActive?: boolean;
  onOpenProvider: (vendor: string) => void;
  onRemove: (vendor: string, model: string) => void;
  onUnhide: (vendor: string, model: string) => void;
  onAskDisconnect: () => void;
  onDisconnect: (vendor: string) => void;
  onBack: () => void;
  onClose: () => void;
}

export function ProvidersScreen(props: ProvidersScreenProps): React.ReactElement {
  const { ui, supported, width, isActive = true } = props;
  const frame = (title: string, body: React.ReactNode) => (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        {title}
      </Text>
      {body}
    </Box>
  );

  if (ui.stage === "list") {
    const options: ConfirmOption<string>[] = ui.providers.map((row) => ({
      label: sanitizeText(row.label === row.vendor ? row.vendor : `${row.label} (${row.vendor})`),
      badge: providerBadges(row) || undefined,
      value: row.vendor,
    }));
    options.push({ label: say("Close", "닫기"), value: "" });
    const warnings = ui.providers.flatMap((row) => hiddenInUseLines(row).map((line) => `${row.vendor}: ${line}`));
    return frame(
      say("Providers", "제공자"),
      <>
        {ui.providers.length === 0 ? <Text dimColor>{say("no provider is connected", "연결된 제공자가 없어요")}</Text> : null}
        {warnings.map((line) => (
          <Text key={line} color="yellow" wrap="wrap">
            {line}
          </Text>
        ))}
        <ConfirmMenu<string>
          key="list"
          options={options}
          escapeValue=""
          isActive={isActive}
          windowSize={PROVIDERS_ROWS}
          onChoose={(vendor) => (vendor ? props.onOpenProvider(vendor) : props.onClose())}
        />
      </>,
    );
  }

  const row = ui.providers.find((item) => item.vendor === ui.vendor);
  if (!row) return frame(say("Providers", "제공자"), <Text dimColor>{ui.vendor}</Text>);

  if (ui.stage === "confirm") {
    return frame(
      say(`Disconnect ${sanitizeText(row.label)}`, `${sanitizeText(row.label)} 연결 끊기`),
      <>
        <Text wrap="wrap">{disconnectConfirmText(row)}</Text>
        <ConfirmMenu<boolean>
          key="confirm"
          options={[
            { label: say("Cancel", "취소"), value: false },
            { label: say("Disconnect", "연결 끊기"), value: true, danger: true },
          ]}
          escapeValue={false}
          isActive={isActive}
          onChoose={(yes) => (yes ? props.onDisconnect(row.vendor) : props.onBack())}
        />
      </>,
    );
  }

  const menu = providerMenu(row, ui.models, supported);
  const options: ConfirmOption<ProviderAction>[] = menu.map((item) => ({
    label: item.label,
    hint: item.hint,
    danger: item.danger,
    value: item.action,
  }));
  const choose = (action: ProviderAction) => {
    if (action.kind === "remove") props.onRemove(row.vendor, action.model);
    else if (action.kind === "unhide") props.onUnhide(row.vendor, action.model);
    else if (action.kind === "disconnect") props.onAskDisconnect();
    else props.onBack();
  };
  return frame(
    sanitizeText(row.label === row.vendor ? row.vendor : `${row.label} (${row.vendor})`),
    <>
      {hiddenInUseLines(row).map((line) => (
        <Text key={line} color="yellow" wrap="wrap">
          {line}
        </Text>
      ))}
      {supported ? (
        <Text dimColor wrap="wrap">
          {say(
            "Enter on a model removes it from this provider's lists; hidden ones can be shown again.",
            "모델에서 Enter 를 누르면 이 제공자 목록에서 빠져요. 숨긴 모델은 다시 보이게 할 수 있어요.",
          )}
        </Text>
      ) : null}
      <ConfirmMenu<ProviderAction>
        key={`provider-${row.vendor}-${menu.length}`}
        options={options}
        escapeValue={{ kind: "back" }}
        isActive={isActive}
        windowSize={PROVIDERS_ROWS}
        onChoose={choose}
      />
    </>,
  );
}
