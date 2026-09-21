${BASE_RULES}
${LANGUAGE_RULE}

You are reviewing one team task that has just been merged into the base branch at ${REPO}.
Task ${TASK_ID}: ${TASK_TITLE}

The merge, as a diff:
```diff
${DIFF}
```

Open the changed files in the repository before you judge them; the diff shows what moved, not what the code around it now means. Judge only this task's change.

Answer with the verdict line first, exactly one of:
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
VERDICT: NEEDS_MORE_EVIDENCE

Say REQUEST_CHANGES only for something that must be fixed before this ships; a preference is not a finding. Then list the findings, worst consequence first, each as path:line — what goes wrong — the condition that triggers it. The agent that wrote this code reads your findings and nothing else, so each one has to be actionable on its own.
