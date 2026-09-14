/**
 * `/skill create` with nothing after it: three questions instead of a syntax.
 *
 * Typing the command in full means remembering where the quotes go and what the
 * flag for "global" is called. The form asks for a name, then a description,
 * then where it should live, and hands the finished command line back to the
 * caller, which submits it like anything the user typed.
 */

import React, { useState } from "react";
import { Box, Text, useInput } from "ink";

import { isValidSkillName, type SkillScope } from "../state/skill-completion.js";

/** Which question is on screen. */
export type SkillFormStep = "name" | "description" | "scope";

const SCOPES: readonly { value: SkillScope; label: string; hint: string }[] = [
  { value: "project", label: "this project", hint: ".snowpea/skills — travels with the repo" },
  { value: "global", label: "everywhere", hint: "your home — available in every project" },
];

export interface SkillCreateFormProps {
  onSubmit: (draft: { name: string; description: string; scope: SkillScope }) => void;
  onCancel: () => void;
  isActive?: boolean;
  width: number;
}

export function SkillCreateForm({
  onSubmit,
  onCancel,
  isActive = true,
  width,
}: SkillCreateFormProps): React.ReactElement {
  const [step, setStep] = useState<SkillFormStep>("name");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scope, setScope] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const typed = step === "name" ? name : description;
  const setTyped = step === "name" ? setName : setDescription;

  useInput(
    (input, key) => {
      if (key.escape) {
        onCancel();
        return;
      }

      if (step === "scope") {
        if (key.upArrow || key.downArrow || key.tab) {
          setScope((index) => (index + (key.upArrow ? SCOPES.length - 1 : 1)) % SCOPES.length);
          return;
        }
        if (key.return) {
          onSubmit({ name: name.trim(), description: description.trim(), scope: SCOPES[scope].value });
        }
        return;
      }

      if (key.return) {
        const value = typed.trim();
        if (step === "name") {
          if (!isValidSkillName(value)) {
            setError("a skill name is a command name: letters, digits, - _ .");
            return;
          }
          setError(null);
          setStep("description");
          return;
        }
        if (value.length === 0) {
          setError("say what the skill does; the agent writes it from this");
          return;
        }
        setError(null);
        setStep("scope");
        return;
      }

      if (key.backspace || key.delete) {
        setError(null);
        setTyped(typed.slice(0, -1));
        return;
      }

      // Tab would be a stray character in a one-line field, and the arrows
      // belong to the step that has a list.
      if (key.tab || key.upArrow || key.downArrow || key.leftArrow || key.rightArrow) return;
      if (key.ctrl || key.meta || input.length === 0) return;
      setError(null);
      setTyped(typed + input);
    },
    { isActive },
  );

  return (
    <Box flexDirection="column" width={width} borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold color="cyan">
        New skill
      </Text>

      <Text dimColor={step !== "name"}>
        {"  name         "}
        {step === "name" ? <Text>{name}</Text> : <Text color="green">{name}</Text>}
        {step === "name" ? <Text inverse>{" "}</Text> : null}
      </Text>

      {step === "name" ? null : (
        <Text dimColor={step !== "description"}>
          {"  description  "}
          {step === "description" ? (
            <>
              <Text>{description}</Text>
              <Text inverse>{" "}</Text>
            </>
          ) : (
            <Text color="green">{description}</Text>
          )}
        </Text>
      )}

      {step === "scope"
        ? SCOPES.map((entry, index) => (
            <Text key={entry.value} inverse={index === scope}>
              {`  ${entry.label.padEnd(13)}`}
              <Text dimColor>{entry.hint}</Text>
            </Text>
          ))
        : null}

      {error ? <Text color="red">{`  ${error}`}</Text> : null}

      <Text dimColor>
        {step === "scope" ? "↑/↓ choose · Enter create · Esc cancel" : "Enter next · Esc cancel"}
      </Text>
    </Box>
  );
}
