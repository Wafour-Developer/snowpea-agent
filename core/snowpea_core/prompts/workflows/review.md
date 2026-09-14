${BASE_RULES}
${LANGUAGE_RULE}

Review the change that is currently in the working tree at ${WORKDIR}.
${FOCUS}
Changed files:
${FILES}

The diff as it stands:
```diff
${DIFF}
```

Open the changed files themselves before you judge them — the diff shows what moved, not what the code around it now means. Judge only this change.

Answer with the verdict line first, exactly one of:
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
VERDICT: NEEDS_MORE_EVIDENCE

Then the findings, worst consequence first, each as path:line — what goes wrong — the condition that triggers it. Close with one line naming what you checked and what you could not check.
