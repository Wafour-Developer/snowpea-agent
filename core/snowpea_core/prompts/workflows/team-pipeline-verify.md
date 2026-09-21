${BASE_RULES}
${LANGUAGE_RULE}
Verify the team pipeline evidence for: ${TASK}

Tests reported: ${TESTS}

Changed files:
${FILES}

${HANDOFF}

The change, as a diff:
```diff
${DIFF}
```

Inspect the changed files in the project at ${WORKDIR} and check whether the test evidence and implementation claims are enough to trust this run. This is an evidence audit, not a second code review.

Answer with the verdict line first, exactly one of:
VERIFY: PASS
VERIFY: FAIL

Then give the evidence you checked. Include a ```handoff ... ``` block (10-20 lines) in your answer:
```handoff
Decided: <verification checks performed>
Files touched: <verified files>
Findings: <audit findings and evidence matches>
Remaining: <unverified areas or "nothing">
Risks: <residual risks or missing coverage>
```

If the files, tool results, or test evidence are missing, say VERIFY: FAIL and explain what is missing.

If an approval you needed was denied, or a tool call failed, say so plainly and answer VERIFY: FAIL. A verdict with nothing behind it is recorded as needing more evidence, not as a pass.
