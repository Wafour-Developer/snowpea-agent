---
name: hello
description: Greet whoever the arguments name.
argument-hint: "[name]"
user-invocable: true
allowed-tools: [read_file, glob]
---

Greet $ARGUMENTS in one short sentence, then stop. Do not use any tool that is
not needed to write the greeting.
