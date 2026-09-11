# vendor/patches

Every vendored file that snowpea modifies keeps a unified diff here. The patch
file mirrors the destination path **relative to `core/snowpea_core/vendor/hermes/`**,
with `.patch` appended, so subdirectories are preserved:

```
core/snowpea_core/vendor/hermes/tools/terminal_tool.py
  -> core/snowpea_core/vendor/patches/tools/terminal_tool.py.patch
```

A file with no local modifications has no patch file.

## What the diff is taken against

The diff runs from the **pristine upstream bytes** (the file as it exists in the
read-only reference clone at `$HERMES_REF`, default `/tmp/hermes-ref`) to the
**working copy in this repository**. The provenance header line therefore shows
up as an added line inside the patch itself. Diff headers use `a/<rel>` and
`b/<rel>` paths so `git apply -p1` and `patch -p1` both work.

## Integrity rule

Verification is **not** byte-equality with upstream (§2.4 of the consensus plan).
A vendored file is intact when:

1. `sha256` of `$HERMES_REF/<upstream path>` matches the value recorded in
   `docs/vendoring-map.json`, and
2. applying the committed patch to those upstream bytes reproduces the working
   copy **byte for byte** — or, when there is no patch, the working copy is
   exactly the header line plus a newline plus the upstream bytes, and
3. the working copy's first line is the provenance header, and
4. no file under `core/snowpea_core/vendor/hermes/` is absent from the map.

`scripts/verify_vendor_integrity.py` enforces all four in the `vendor-integrity`
CI job.

## Regenerating a patch

After editing a vendored file:

```bash
HERMES_REF=/tmp/hermes-ref python3 scripts/verify_vendor_integrity.py \
    --update-patch core/snowpea_core/vendor/hermes/tools/terminal_tool.py
```

That rewrites the patch, updates `docs/vendoring-map.json`, and re-renders
`docs/vendoring-map.md`.
