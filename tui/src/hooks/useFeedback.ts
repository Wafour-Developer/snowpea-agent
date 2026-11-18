/**
 * `/feedback` (alias `/개선하기`) for the current session (capability
 * `improvementFeedback`, protocol 1.59.0).
 *
 * Opening reads `feedback.preview` (nothing leaves the machine); the screen
 * sends with `feedback.send` only after the person picks Send. A daemon without
 * the capability gets one "requires a newer core" line and no call at all.
 */

import { useCallback, useRef, useState } from "react";

import type { TuiClient } from "../rpc/client.js";
import { TUI_VERSION } from "../version.js";
import {
  FEEDBACK_REQUIRES_LINE,
  feedbackErrorLine,
  feedbackSupported,
  normalizePreview,
  parseFeedbackCommand,
  sentLine,
  type FeedbackPreviewView,
  type FeedbackSendOutcome,
} from "../state/feedback.js";
import { say } from "../state/errors.js";

export interface FeedbackUi {
  preview: FeedbackPreviewView;
  /** What the person typed after the command, the comment's first draft. */
  comment: string;
}

export interface FeedbackDeps {
  client: TuiClient;
  sessionId: string;
  /** A plain line in the main transcript. */
  note: (text: string) => void;
  showToast: (text: string) => void;
}

export function useFeedback(deps: FeedbackDeps) {
  const { client, sessionId, note, showToast } = deps;
  const supported = feedbackSupported(client.serverCapabilities?.());
  const [ui, setUi] = useState<FeedbackUi | null>(null);
  const opening = useRef(false);

  const open = useCallback(
    async (comment: string) => {
      if (opening.current) return;
      opening.current = true;
      showToast(say("Preparing the preview…", "미리보기를 준비하는 중…"));
      try {
        setUi({ preview: normalizePreview(await client.call("feedback.preview", { sessionId })), comment });
      } catch (error) {
        note(feedbackErrorLine(error));
      } finally {
        opening.current = false;
      }
    },
    [client, note, sessionId, showToast],
  );

  /** `/feedback [comment]`, `/개선하기 [comment]`; true when the line was one. */
  const handleCommand = useCallback(
    (line: string): boolean => {
      const command = parseFeedbackCommand(line);
      if (!command) return false;
      if (!supported) {
        note(FEEDBACK_REQUIRES_LINE());
        return true;
      }
      void open(command.comment);
      return true;
    },
    [note, open, supported],
  );

  /** The upload, once; the outcome is also left in the transcript. */
  const send = useCallback(
    async (comment: string): Promise<FeedbackSendOutcome> => {
      try {
        const result = (await client.call("feedback.send", {
          sessionId,
          surface: "tui",
          surfaceVersion: TUI_VERSION,
          ...(comment.trim() ? { comment: comment.trim() } : {}),
        })) as { id?: unknown } | null;
        const id = typeof result?.id === "string" ? result.id : "";
        note(sentLine(id));
        return { ok: true, id };
      } catch (error) {
        const message = feedbackErrorLine(error);
        note(message);
        return { ok: false, message };
      }
    },
    [client, note, sessionId],
  );

  return {
    supported,
    ui,
    close: useCallback(() => setUi(null), []),
    handleCommand,
    send,
  };
}

export type FeedbackHook = ReturnType<typeof useFeedback>;
