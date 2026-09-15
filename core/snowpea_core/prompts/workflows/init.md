You are running snowpea's `/init`: fast and rough, like Claude Code's `/init`, not a deep audit (that is `/deepinit`).

In at most 8 tool calls, look at the root of this project: the README, a package manifest (package.json, pyproject.toml, go.mod, Cargo.toml, requirements.txt, or similar), the top-level directory layout, any existing AGENTS.md or CLAUDE.md, and CI configuration (.github/workflows, .gitlab-ci.yml, or similar).

${AGENTS_STATUS}

Then write ${AGENTS_PATH}: at most 60 lines covering, in this order — what the project is, how to run/build/test/lint it, the top-level layout, conventions worth knowing, and anything an agent should avoid. Use write_file.

${SETTINGS_NOTE}

${BASE_RULES}
${LANGUAGE_RULE}

Finish with exactly three lines summarising what you wrote. (Plan mode may write AGENTS.md — it is a document — but not the settings file; say so if that applies.)
