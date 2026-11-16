"""Security lists: blocked commands and protected folders (protocol 1.54.0).

A general snowpea feature, with or without Jarvis mode.  A command on the
blocked list and a change of a protected folder are refused wherever snowpea
reads them — a call's command line, the code on it and the scripts it names —
for everything that goes through snowpea: its own tools in every mode, the
approval queue, external CLIs' bridged approvals, Jarvis and scheduled
workers.  What a program started that way loads or runs in turn (tests,
builds, git hooks, a program found on PATH) is not read (security review 8).

* :mod:`.catalog` — the preset catalog (en, ko).
* :mod:`.patterns` — the custom command pattern language.
* :mod:`.shell` — what a shell line runs and touches, seen through the
  classifier's parser.
* :mod:`.lists` — the settings, the effective lists and their validation.
* :mod:`.check` — the pure judgement (``security.check``).
* :mod:`.enforce` — the daemon side: the gate every caller uses, the event and
  the audit line.
"""

from __future__ import annotations
