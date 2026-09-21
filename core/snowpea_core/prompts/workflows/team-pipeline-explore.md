${BASE_RULES}
${LANGUAGE_RULE}
The team is about to work on: ${TASK}

Survey the project at ${WORKDIR} and report what the agents that plan and implement this need to know: where the relevant code lives, which files will have to change, the conventions they must follow, and anything that makes the obvious approach wrong.

Read; do not change anything. When you are done, answer with at most 20 lines inside a ```handoff ... ``` block:
```handoff
Decided: <conventions or architecture choices to follow>
Files touched: none (exploration only)
Findings: <findings with path:line, not prose>
Remaining: <what still needs investigation or "nothing">
Risks: <risks, pitfalls, or edge cases>
```
