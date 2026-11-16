"""Security lists: blocked commands and protected folders (protocol 1.54.0).

A general snowpea feature, with or without Jarvis mode.  A command on the
blocked list never runs and a protected folder is never changed by anything
that goes through snowpea: snowpea's own tools in every mode, the approval
queue, external CLIs' bridged approvals, Jarvis and scheduled workers.

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
