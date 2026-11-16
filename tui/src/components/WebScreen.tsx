/**
 * `/web`: Remote access, one full-screen overlay.
 *
 * It shows whether snowpea web is open to other machines, where, who is signed
 * in and why it is not listening when it should be; starts and stops the
 * listener; holds the access link with its token masked (revealed on request,
 * copied with OSC 52 where the terminal takes it, printed once where not);
 * rotates it; sets and clears a password with hidden input typed twice. The
 * plain-HTTP warning is always on screen.
 *
 * It owns the keyboard while it is open. The password is held only while it is
 * being typed: it goes to the daemon and is dropped, never into a draft, a
 * history line or the transcript. Lines are drawn from the pure functions in
 * `state/web-remote.ts` and wrapped, so nothing is cut at 60 columns.
 */

import React, { useEffect, useRef, useState } from "react";
import { Box, useInput } from "ink";

import type { CopyResult, WebResult } from "../hooks/useWebScreen.js";
import { mouseReports } from "../input/mouse.js";
import type { Line, Segment } from "../layout/transcript.js";
import { rpcCode } from "../state/errors.js";
import { wrapSegments } from "../state/security.js";
import {
  MAX_MODE_EXPLAIN,
  MIN_PASSWORD,
  PLAIN_HTTP_WARNING,
  maxModeRow,
  nextMaxMode,
  type MaxMode,
  RESTART_LINE,
  fieldError,
  hidden,
  initialForm,
  listenLine,
  localPersonOnlyLine,
  modeLine,
  passwordProblem,
  remoteJobsLine,
  remoteLeftLine,
  sessionsLine,
  shownLink,
  validateForm,
  type FormErrors,
  type ListenField,
  type ListenForm,
  type WebStatus,
} from "../state/web-remote.js";
import { degradedLine } from "../state/settings-degraded.js";
import { RenderedLines } from "./RenderedLines.js";

export interface WebScreenProps {
  status: WebStatus;
  width: number;
  height: number;
  onListen: (form: ListenForm) => Promise<WebResult>;
  onStop: (save: boolean) => Promise<WebResult>;
  onRotate: () => Promise<WebResult>;
  onSetPassword: (password: string, username: string) => Promise<WebResult>;
  onClearPassword: () => Promise<WebResult>;
  /** Sets the highest mode a remote sign-in may use (`settings.set web.remote.maxMode`). */
  onSetMaxMode?: (mode: MaxMode) => Promise<WebResult>;
  /** The line saying remote access settings wait on disk (`/settings disk`), or null. */
  diskNotice?: string | null;
  onRefresh: () => Promise<WebResult>;
  /** Put the link on the clipboard when the terminal obeys OSC 52. */
  onCopy: (link: string) => CopyResult;
  /** Counts `settings.changed` events that touched `web`. */
  changeSeq?: number;
  onClose: () => void;
  isActive?: boolean;
}

interface Note {
  text: string;
  color?: string;
  dim?: boolean;
}

type FieldIndex = 0 | 1 | 2 | 3 | 4;
const FIELDS: ReadonlyArray<ListenField | "remember" | "submit"> = ["host", "port", "username", "remember", "submit"];

type Mode =
  | { k: "browse" }
  | { k: "form"; form: ListenForm; at: FieldIndex; errors: FormErrors; message: string | null }
  | { k: "confirm"; lines: string[]; keys: string; options: Array<{ key: string; run: () => void }> }
  | { k: "password"; step: "username" | "password" | "repeat"; username: string; first: string; value: string }
  | { k: "busy"; what: string };

export function WebScreen({
  status: initial,
  width,
  height,
  onListen,
  onStop,
  onRotate,
  onSetPassword,
  onClearPassword,
  onSetMaxMode,
  diskNotice = null,
  onRefresh,
  onCopy,
  changeSeq = 0,
  onClose,
  isActive = true,
}: WebScreenProps): React.ReactElement {
  const [status, setStatus] = useState<WebStatus>(initial);
  const [link, setLink] = useState<string | null>(initial.accessUrl);
  const [revealed, setRevealed] = useState(false);
  const [mode, setMode] = useState<Mode>({ k: "browse" });
  const [notes, setNotes] = useState<Note[]>([]);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const say = (text: string, color?: string) => setNotes([{ text, color }]);
  const applyStatus = (next: WebStatus, replaceLink?: string | null) => {
    setStatus(next);
    setLink(replaceLink !== undefined ? replaceLink : next.accessUrl);
  };

  // The settings changed elsewhere: read the status again while nothing is being typed.
  const idle = useRef(true);
  idle.current = mode.k === "browse";
  useEffect(() => {
    if (changeSeq === 0 || !idle.current) return;
    void onRefresh().then((result) => {
      if (mounted.current && result.ok) applyStatus(result.status);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changeSeq]);

  /** Run a call; `done` gets the new status. A refusal by a non-local connection ends the screen's work. */
  const act = (what: string, call: () => Promise<WebResult>, done: (result: Extract<WebResult, { ok: true }>) => void, failed?: (error: unknown) => boolean) => {
    setMode({ k: "busy", what });
    setNotes([{ text: `${what}…`, dim: true }]);
    void call().then((result) => {
      if (!mounted.current) return;
      if (result.ok) {
        setMode({ k: "browse" });
        done(result);
        return;
      }
      if (failed?.(result.error)) return;
      setMode({ k: "browse" });
      say(rpcCode(result.error) === "jarvis_person_only" ? localPersonOnlyLine(result.error) : `${what} failed: ${String((result.error as Error)?.message ?? result.error)}`, "red");
    });
  };

  const startForm = () => {
    setNotes([]);
    setMode({ k: "form", form: initialForm(status), at: 0, errors: {}, message: null });
  };

  const submitForm = (current: Extract<Mode, { k: "form" }>) => {
    const errors = validateForm(current.form);
    if (Object.keys(errors).length > 0) {
      const first = FIELDS.findIndex((name) => name in errors) as FieldIndex;
      setMode({ ...current, errors, at: first, message: null });
      return;
    }
    const form = current.form;
    setMode({ k: "busy", what: "Opening the listener" });
    setNotes([{ text: "Opening the listener…", dim: true }]);
    void onListen(form).then((result) => {
      if (!mounted.current) return;
      if (result.ok) {
        applyStatus(result.status);
        setMode({ k: "browse" });
        say(
          result.status.listening
            ? `Listening on ${result.status.host}:${result.status.port}.${form.remember ? " It will start again with the daemon." : ""}`
            : listenLine(result.status),
          result.status.listening ? "green" : "yellow",
        );
        return;
      }
      if (rpcCode(result.error) === "jarvis_person_only") {
        setMode({ k: "browse" });
        say(localPersonOnlyLine(result.error), "yellow");
        return;
      }
      // The refusal goes on the field it belongs to; the form stays open with what was typed.
      const { field, message } = fieldError(result.error);
      const at = (field ? FIELDS.indexOf(field) : 4) as FieldIndex;
      setMode({ k: "form", form, at, errors: field ? { [field]: message } : {}, message: field ? null : message });
    });
  };

  const confirm = (lines: string[], options: Array<{ key: string; run: () => void }>, keys: string) => {
    setNotes([]);
    setMode({ k: "confirm", lines, keys, options });
  };

  const askStop = () => {
    if (!status.listening) {
      say("It is not listening.", "yellow");
      return;
    }
    const busy = status.turnsRunning > 0 ? `${status.turnsRunning} turn${status.turnsRunning === 1 ? " is" : "s are"} running; people watching them lose the view.` : null;
    confirm(
      ["Stop listening?", "Everyone signed in through it is signed out.", ...(busy ? [busy] : []), "y stop now · k stop and keep it off at the next start · n cancel"],
      [
        { key: "y", run: () => doStop(false) },
        { key: "k", run: () => doStop(true) },
      ],
      "ykn",
    );
  };
  const doStop = (save: boolean) =>
    act("Stopping", () => onStop(save), (result) => {
      applyStatus(result.status);
      setRevealed(false);
      say(save ? "Stopped. It stays off at the next start." : "Stopped. Everyone was signed out.", "green");
    });

  const askRotate = () =>
    confirm(
      ["Make a new access link?", "The old link stops working and everyone is signed out.", "Make a new one? (y/n)"],
      [{ key: "y", run: () => doRotate() }],
      "yn",
    );
  const doRotate = () =>
    act("Making a new link", onRotate, (result) => {
      applyStatus(result.status, result.link ?? result.status.accessUrl);
      setRevealed(false);
      say("New access link made. The old one stopped working and everyone was signed out.", "green");
    });

  const askClear = () => {
    if (!status.passwordSet) {
      say("No password is set.", "yellow");
      return;
    }
    confirm(
      ["Remove the password?", "Sign-in goes back to the access link and everyone is signed out.", "Remove it? (y/n)"],
      [{ key: "y", run: () => doClear() }],
      "yn",
    );
  };
  const doClear = () =>
    act("Removing the password", onClearPassword, (result) => {
      applyStatus(result.status);
      say("Password removed. Sign-in is the access link again; everyone was signed out.", "green");
    });

  const startPassword = () => {
    setNotes([]);
    setMode({ k: "password", step: status.username === "" ? "username" : "password", username: status.username, first: "", value: "" });
  };

  const submitPassword = (current: Extract<Mode, { k: "password" }>) => {
    if (current.step === "username") {
      if (current.value.trim() === "") {
        say("Type a username: password mode needs one.", "red");
        return;
      }
      setNotes([]);
      setMode({ k: "password", step: "password", username: current.value.trim(), first: "", value: "" });
      return;
    }
    if (current.step === "password") {
      if (Array.from(current.value).length < MIN_PASSWORD) {
        say(`A password needs at least ${MIN_PASSWORD} characters.`, "red");
        setMode({ ...current, value: "" });
        return;
      }
      setNotes([]);
      setMode({ k: "password", step: "repeat", username: current.username, first: current.value, value: "" });
      return;
    }
    const problem = passwordProblem(current.first, current.value);
    if (problem) {
      // Both entries are dropped: it is typed again from the start.
      setMode({ k: "password", step: "password", username: current.username, first: "", value: "" });
      say(problem, "red");
      return;
    }
    const password = current.first;
    const username = current.username;
    act("Setting the password", () => onSetPassword(password, username), (result) => {
      applyStatus(result.status);
      say("Password set. Sign-in now needs the username and the password; everyone was signed out.", "green");
    }, (error) => {
      setMode({ k: "password", step: "password", username, first: "", value: "" });
      say(`The password was not set: ${String((error as Error)?.message ?? error)}`, "red");
      return true;
    });
  };

  const copyLink = () => {
    if (!link) {
      say(status.mode === "password" ? "In password mode there is no access link: people sign in with the username and password." : "There is no access link while it is not listening.", "yellow");
      return;
    }
    if (onCopy(link) === "copied") {
      say("Copied the access link to the clipboard (OSC 52).", "green");
      return;
    }
    // Printed once, on request: the next key press clears it.
    setNotes([
      { text: "This terminal is not known to take OSC 52 copies. The link, once (select it with the mouse):", color: "yellow" },
      { text: link, color: "cyan" },
    ]);
  };

  useInput(
    (input, key) => {
      if (mouseReports(input).length > 0) return;
      if (mode.k === "busy") return;

      if (mode.k === "confirm") {
        const pressed = input.toLowerCase();
        const chosen = mode.options.find((option) => option.key === pressed);
        if (chosen) {
          setMode({ k: "browse" });
          chosen.run();
        } else if (pressed === "n" || key.escape) {
          setMode({ k: "browse" });
        }
        return;
      }

      if (mode.k === "password") {
        if (key.escape) {
          setMode({ k: "browse" });
          setNotes([]);
        } else if (key.return) submitPassword(mode);
        else if (key.backspace || key.delete) setMode({ ...mode, value: Array.from(mode.value).slice(0, -1).join("") });
        else if (input && !key.ctrl && !key.meta && !key.tab && !key.upArrow && !key.downArrow) {
          // A pasted password arrives as one chunk; line breaks never belong to it.
          setMode({ ...mode, value: mode.value + input.replace(/[\r\n]+/g, "") });
        }
        return;
      }

      if (mode.k === "form") {
        const name = FIELDS[mode.at];
        const move = (to: number) => setMode({ ...mode, at: ((to + FIELDS.length) % FIELDS.length) as FieldIndex });
        if (key.escape) {
          setMode({ k: "browse" });
          setNotes([]);
        } else if (key.tab && key.shift) move(mode.at - 1);
        else if (key.tab || key.downArrow) move(mode.at + 1);
        else if (key.upArrow) move(mode.at - 1);
        else if (key.return) {
          if (name === "submit") submitForm(mode);
          else move(mode.at + 1);
        } else if (name === "remember") {
          if (input === " ") setMode({ ...mode, form: { ...mode.form, remember: !mode.form.remember } });
        } else if (name === "submit") {
          return;
        } else if (key.backspace || key.delete) {
          setMode({ ...mode, form: { ...mode.form, [name]: Array.from(mode.form[name]).slice(0, -1).join("") }, errors: { ...mode.errors, [name]: undefined }, message: null });
        } else if (input && !key.ctrl && !key.meta && !/\s/.test(input)) {
          setMode({ ...mode, form: { ...mode.form, [name]: mode.form[name] + input }, errors: { ...mode.errors, [name]: undefined }, message: null });
        }
        return;
      }

      // browse
      setNotes([]);
      if (key.escape || input === "q") {
        onClose();
        return;
      }
      switch (input.toLowerCase()) {
        case "l":
          return startForm();
        case "x":
          return askStop();
        case "t":
          return askRotate();
        case "p":
          return startPassword();
        case "c":
          return askClear();
        case "v":
          if (!link) return copyLink();
          setRevealed((on) => !on);
          return;
        case "y":
          return copyLink();
        case "m": {
          if (status.maxMode === null || !onSetMaxMode) return;
          const next = nextMaxMode(status.maxMode);
          return act(
            "Setting the highest mode",
            () => onSetMaxMode(next),
            (result) => {
              applyStatus(result.status);
              say(`Remote sign-ins are limited to ${result.status.maxMode ?? next} from now on; it applies at once.`, "green");
            },
          );
        }
        case "r":
          return act("Reading the status", onRefresh, (result) => applyStatus(result.status));
        default:
      }
    },
    { isActive },
  );

  // --- drawing -----------------------------------------------------------------

  const inner = Math.max(20, width - 4);
  const rowsLeft = Math.max(6, height - 2);
  const bold = (text: string, color?: string): Segment => ({ text, bold: true, color });
  const wrap = (key: string, segments: Segment[], indent = "  "): Line[] => wrapSegments(key, segments, inner, indent);

  const head: Line[] = wrap("title", [bold("Remote access", "cyan")], "");

  const body: Line[] = [];
  body.push(...wrap("listen", [bold(listenLine(status), status.listening ? "green" : status.error ? "red" : "yellow")]));
  if (status.listening) {
    status.listenUrls.slice(0, 3).forEach((url, index) => body.push(...wrap(`url-${index}`, [{ text: "Open  ", dimColor: true }, { text: url, color: "cyan" }], "        ")));
    body.push(...wrap("sessions", [{ text: sessionsLine(status) }]));
  }
  const degraded = degradedLine(status.settingsDegraded);
  const remoteJobs = remoteJobsLine(status);
  if (remoteJobs) body.push(...wrap("remote-jobs", [{ text: remoteJobs, color: "yellow" }]));
  const remoteLeft = remoteLeftLine(status);
  if (remoteLeft) body.push(...wrap("remote-left", [{ text: remoteLeft, color: "yellow" }]));
  body.push(...wrap("mode", [{ text: modeLine(status) }]));
  if (status.listening && status.mode === "token") {
    body.push(
      ...wrap(
        "link",
        link
          ? [{ text: "Link  ", dimColor: true }, { text: shownLink(link, revealed), color: "cyan" }]
          : [{ text: "No access link is available.", dimColor: true }],
        "        ",
      ),
    );
    if (link) body.push(...wrap("link-keys", [{ text: revealed ? "v hides the token · y copies the link" : "v reveals the token · y copies the link", dimColor: true }]));
  }
  if (status.maxMode !== null) {
    body.push(...wrap("max-mode", [{ text: maxModeRow(status.maxMode), bold: true }], "  "));
    body.push(...wrap("max-mode-why", [{ text: MAX_MODE_EXPLAIN(), dimColor: true }], "  "));
  }
  if (status.needsRestart) body.push(...wrap("restart", [{ text: RESTART_LINE, color: "yellow" }]));
  // Core's warning sits at the top and is never the one to go: the status below it gives way first.
  const warning: Line[] = [
    ...wrap("warn", [bold("! ", "yellow"), { text: status.warning ?? PLAIN_HTTP_WARNING, color: "yellow" }], "  "),
    ...(diskNotice ? wrap("disk", [bold("! ", "yellow"), { text: diskNotice, color: "yellow" }], "  ") : []),
    ...(degraded ? wrap("degraded", [bold("! ", "yellow"), { text: degraded, color: "yellow" }], "  ") : []),
  ];

  const live: Line[] = [];
  const hintParts: string[] = [];
  if (mode.k === "form") {
    const { form, errors, message, at } = mode;
    const field = (index: FieldIndex, label: string, value: string, name: ListenField, placeholder?: string) => {
      const here = at === index;
      live.push(
        ...wrap(
          `f-${name}`,
          [
            { text: here ? "› " : "  ", color: "cyan", bold: true },
            { text: `${label.padEnd(9)}`, bold: here, dimColor: !here },
            { text: value === "" && placeholder ? placeholder : value, color: value === "" ? undefined : "white", dimColor: value === "" },
            ...(here ? [{ text: "▏", color: "cyan" }] : []),
          ],
          "              ",
        ),
      );
      const problem = errors[name];
      if (problem) live.push(...wrap(`e-${name}`, [{ text: "    ✗ ", color: "red", bold: true }, { text: problem, color: "red" }], "      "));
    };
    live.push(...wrap("form-title", [bold("Listen for other machines")], ""));
    field(0, "Address", form.host, "host");
    field(1, "Port", form.port, "port");
    field(2, "Username", form.username, "username", "(optional — for password mode)");
    live.push(
      ...wrap(
        "f-remember",
        [
          { text: at === 3 ? "› " : "  ", color: "cyan", bold: true },
          { text: form.remember ? "[x] " : "[ ] ", color: form.remember ? "green" : undefined },
          { text: "Remember for next start", bold: at === 3 },
        ],
        "      ",
      ),
    );
    live.push(...wrap("f-submit", [{ text: at === 4 ? "› " : "  ", color: "cyan", bold: true }, bold("Start listening", at === 4 ? "green" : undefined)], "      "));
    if (message) live.push(...wrap("f-message", [{ text: "    ✗ ", color: "red", bold: true }, { text: message, color: "red" }], "      "));
    hintParts.push("Tab/↑↓ field", "Space toggles", "Enter next / start", "Esc cancel");
  } else if (mode.k === "confirm") {
    mode.lines.forEach((text, index) => live.push(...wrap(`ask-${index}`, [{ text, color: "yellow", bold: index === mode.lines.length - 1 }])));
    hintParts.push(mode.keys.includes("k") ? "y stop · k stop and keep off · n / Esc cancel" : "y yes · n / Esc no");
  } else if (mode.k === "password") {
    const prompt =
      mode.step === "username"
        ? "Username for sign-in:"
        : mode.step === "password"
          ? `New password (at least ${MIN_PASSWORD} characters, hidden):`
          : "Repeat the password:";
    const typed = mode.step === "username" ? mode.value : hidden(mode.value);
    live.push(...wrap("p-prompt", [{ text: prompt, color: "cyan" }], ""));
    live.push(...wrap("p-typed", [{ text: "› ", color: "cyan", bold: true }, { text: typed }, { text: "▏", color: "cyan" }], "  "));
    hintParts.push("Enter next", "Esc cancel");
  } else if (mode.k === "busy") {
    hintParts.push(`${mode.what}…`);
  } else {
    hintParts.push("l start / move", ...(status.listening ? ["x stop"] : []), "t new link", "p password", ...(status.passwordSet ? ["c clear password"] : []), ...(status.maxMode !== null && onSetMaxMode ? ["m max mode"] : []), "r refresh", "Esc close");
  }

  const foot: Line[] = [];
  notes.forEach((note, index) => foot.push(...wrap(`note-${index}`, [{ text: note.text, color: note.color, dimColor: note.dim, bold: note.color === "red" }])));
  const hint: Line[] = wrap("hint", [{ text: hintParts.join(" · "), dimColor: true }], "");

  // The warning is never the one to go when the terminal is short; the status above it gives way first.
  const fixed = head.length + live.length + foot.length + hint.length + warning.length;
  const room = Math.max(0, rowsLeft - fixed);
  const shownBody = body.length > room ? body.slice(0, room) : body;

  return (
    <Box flexDirection="column" width={width} height={height} borderStyle="round" borderColor={status.listening ? "yellow" : "cyan"} paddingX={1} overflow="hidden">
      <RenderedLines lines={head} />
      <Box flexDirection="column" flexGrow={1} overflow="hidden">
        <RenderedLines lines={[...warning, ...shownBody, ...live]} />
      </Box>
      <RenderedLines lines={foot} />
      <RenderedLines lines={hint} />
    </Box>
  );
}
