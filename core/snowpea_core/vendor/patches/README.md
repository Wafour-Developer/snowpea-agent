# vendor/patches

Every vendored file that snowpea modifies keeps a unified diff here, named after
its destination path with `/` replaced by `__` and a `.patch` suffix.

Integrity rule (§2.4 of the consensus plan): a vendored file is considered intact
when `sha256(original_upstream_file)` matches the value recorded in
`docs/vendoring-map.md` **and** the committed patch in this directory applies
cleanly to that original. Byte-identity with upstream is *not* required.

`scripts/verify_vendor_integrity.py` enforces this in the `vendor-integrity` CI job.
