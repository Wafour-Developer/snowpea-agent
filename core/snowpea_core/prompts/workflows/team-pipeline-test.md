${BASE_RULES}
${LANGUAGE_RULE}

The team has just finished implementing: ${TASK}

These files changed:
${FILES}

${HANDOFF}

Run the project's own checks over that change, and write the tests it is missing when there is a natural place for them. Use the commands this project already uses; do not invent a test runner it does not have.

Answer with the verdict line first, exactly one of:
TESTS: PASS
TESTS: FAIL

Then the evidence: the commands you ran and what they printed, trimmed to the part that matters. A verdict with no command behind it is not a verdict — if you could not run anything, say TESTS: FAIL and say why.
