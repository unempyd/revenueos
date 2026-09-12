# Provenance

This export was assembled by `scripts/build_public.py` from the internal RevenueOS
monorepo. `VENDOR.json` records, for every vendored file, the exact upstream
repository and commit it was copied from and under which licence;
`upstream/MANIFEST.tsv` records the pinned commit and verified licence for every
upstream repository `scripts/fetch-upstream.sh` clones; and `THIRD_PARTY_LICENSES/`
holds the full licence text for each of those upstream repositories. Together these
three make every piece of vendored code in this tree traceable back to its origin
and licence.
