#!/bin/bash
# Builds design/site/smooth-research-site.html from the design in design/site/content:
# renders the diagram pages, generates the site's content, bundles everything into one file.
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v pnpm >/dev/null 2>&1; then
  export PATH="$PWD/bin:$PATH"
fi
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
[ -d node_modules ] || pnpm install --frozen-lockfile
node scripts/render-pages.mjs
node scripts/gen-content.mjs
bash scripts/bundle-artifact.sh
mv bundle.html smooth-research-site.html
echo "site: design/site/smooth-research-site.html ($(du -h --apparent-size smooth-research-site.html | cut -f1))"
