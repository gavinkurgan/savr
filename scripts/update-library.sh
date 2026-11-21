#!/usr/bin/env sh
# Developer-only: refresh the savr recipe library.
#   ./scripts/update-library.sh            # refresh ratings + scrape sources.txt
#   LLM=1 ./scripts/update-library.sh      # also rewrite new recipes with Claude
# Run from the repo root (or inside the container: docker compose run --rm savr scripts/update-library.sh)
set -eu
FLAGS=""
[ "${LLM:-0}" = "1" ] && FLAGS="--llm"
python -m app.cli refresh $FLAGS
if [ -f sources.txt ]; then
  python -m app.cli scrape --from-file sources.txt --max "${MAX:-50}" $FLAGS
fi
[ "${LLM:-0}" = "1" ] && python -m app.cli rewrite --llm --only-missing
python -m app.cli stats
# Push into the app database right away (the app also syncs on its next start).
python -m app.cli sync
