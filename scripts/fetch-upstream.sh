#!/usr/bin/env bash
# Re-clone every upstream repository at the exact commit pinned in upstream/MANIFEST.tsv.
# upstream/*/ is gitignored; run this after a fresh clone of RevenueOS before running scripts/vendor.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT/upstream/MANIFEST.tsv"
tail -n +2 "$MANIFEST" | while IFS=$'\t' read -r dir repo commit _date _license; do
  dest="$ROOT/upstream/$dir"
  if [ -d "$dest/.git" ]; then
    if [ "$(git -C "$dest" rev-parse HEAD)" = "$commit" ]; then echo "ok    $dir"; continue; fi
  else
    git clone -q --filter=blob:none "https://github.com/$repo.git" "$dest"
  fi
  git -C "$dest" fetch -q --depth 1 origin "$commit"
  git -C "$dest" checkout -q "$commit"
  echo "pinned $dir @ ${commit:0:10}"
done
