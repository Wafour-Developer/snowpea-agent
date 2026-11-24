"""CLI hook integrations: install / uninstall / status for claude, codex, cursor, agy.

Kept import-free for the same reason as the parent package: the hook fast path
(:mod:`.hook_report`) lives here.  The install machinery is in :mod:`.manager`.

Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0.
"""
