# Deviations — CORE-update-safety (git-install tracking, the downgrade guard, forced re-checks)

`system.checkUpdate` was written for PyPI installs, where a version string is the whole answer. A
developer running a `git+https://…@main` install has no meaningful version to compare, and the naive
answer — "the remote tip differs, so update" — will happily move a machine *backwards* onto an older
commit. `28ff1ae`, `066422e` and `55bc6ff` make the git path honest. Recorded here per
`docs/design/deviations/README.md`.

1. **The downgrade guard is ancestry-based, not version-based.** `_check_git_branch`
   (`core/snowpea_core/update.py:354-414`) asks GitHub's `compare/<installed>...<latest>` endpoint
   and only reports `available` when `status == "ahead"`. `"behind"` and `"identical"` mean "no
   update"; anything else — a diverged history after a force-push — raises rather than offering a
   replacement. Comparing `__version__` strings cannot work here, because two commits on the same
   branch usually carry the same version.

2. **Any `compare` failure is a hard error, never a silent "no update".** A non-200 from the compare
   endpoint raises `could not verify update ancestry`, which surfaces in the answer's `error` field.
   The tempting alternative — treat an unverifiable comparison as "nothing to do" — would make a
   rate-limited or offline machine quietly stop receiving updates with no signal at all.

3. **A tag-pinned or unrecognised git install returns `None` from provenance and is never moved.**
   `git_install_provenance` (`update.py:142-176`) reads `install.json` together with PEP 610
   `direct_url.json`, and returns `None` unless the resolved ref is one of `_TRACKED_BRANCHES`. A
   user who pinned a tag pinned it on purpose; auto-moving them to `main` would be the update system
   overruling an explicit choice.

4. **`system.update` forces a fresh check, so an explicit request can never act on a stale negative.**
   `update_handlers.py:60` calls `check_update(..., force=True)` before deciding anything, and
   refuses with `started: false` plus an explanation when the fresh answer carries an `error` or is
   not `available`. `066422e` added this after `28ff1ae` made refusal possible: the 24-hour cache is
   right for the startup nudge and wrong for a user who just typed `/update` because they know a fix
   landed. It also mitigates the caching gap in rule 7.

5. **Internal provenance keys are stripped from the RPC result.** `check_update_handler` removes
   `installKey`, `trackingSource` and `configured` before validating `CheckUpdateResult`. They exist
   to key the cache and to describe the local install; publishing them would make cache-implementation
   detail part of a contract that clients would then depend on.

6. **The branch prompt names a release version read at the exact commit — do not reintroduce the tag
   lookup.** `55bc6ff` displayed the repository's latest *tag* alongside the branch SHA so a branch
   user did not see `0.1.2+newsha from v0.1.2+oldsha` for what was really the 0.1.3 release. Two
   commits later `93be684` replaced it with `_git_version_at()` (`update.py:323`), which reads
   `__init__.py` at the update commit itself. The tag approach is wrong in a way that is easy to
   re-derive and hard to notice: the latest tag is not guaranteed to be an ancestor of the branch
   tip, so it can name a version the update does not contain. Either way the lookup is
   presentation-only and wrapped in `try/except` — a failure must never invalidate the ancestry
   result (`update.py:387-398`).

7. **Revision equality is prefix matching, and that can skip the ancestry check.** `_same_revision`
   (`update.py:102`) does `a.startswith(b) or b.startswith(a)`, and the SHA pattern accepts 7–40 hex
   characters, so a 7-character abbreviation is enough to declare two revisions the same. A false
   match short-circuits the `compare` call entirely, reporting a genuinely newer commit as "already
   on this". Recorded known gap (R7): the fix is to require equal-length or full 40-character SHAs on
   both sides.

8. **A positive answer is cached for 24 hours without re-running `compare`** (`update.py:365-367`),
   so a force-push inside the cache window is not re-verified. Recorded known gap (R8), mitigated but
   not closed by rule 4.
