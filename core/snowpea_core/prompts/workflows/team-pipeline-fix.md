${BASE_RULES}
${LANGUAGE_RULE}

A reviewer read the work you did on ${TASK_ID} (${TASK_TITLE}) and asked for changes. Fix exactly what it found, in the project at ${WORKDIR}, and nothing else. Do not restructure anything it did not raise.

Files you own:
${FILES}

The findings:
${FINDINGS}

When you are done, answer with at most 20 lines in this shape:
Decided: <what you changed to answer each finding>
Files: <the files you actually changed>
Remaining: <any finding you did not fix, and why, or "nothing">
