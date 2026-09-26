#!/usr/bin/env bash
# Mirror remote factory runners' Claude Code transcripts (a GCS bucket laid out as
# {runner}/{issue}/{execution}/{session}.jsonl) into a local dir snapshot.py reads.
# Incremental; the bucket has a lifecycle delete, so the snapshot ledger is the durable copy.
# Config (env, or a gitignored .env.factory next to this script):
#   TOKEN_BURN_FACTORY_BUCKET   e.g. gs://my-factory-transcripts   (unset => no-op)
#   TOKEN_BURN_FACTORY_ROOT     default ~/.token-burn/factory-transcripts
#   CLOUDSDK_CONFIG             gcloud config dir with read access to the bucket
set -euo pipefail
cd "$(dirname "$0")"
# cron's PATH is /usr/bin:/bin, whose python3 is too old for gcloud: prefer Homebrew/local.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
[ -f .env.factory ] && set -a && . ./.env.factory && set +a
[ -n "${TOKEN_BURN_FACTORY_BUCKET:-}" ] || exit 0
DEST="${TOKEN_BURN_FACTORY_ROOT:-$HOME/.token-burn/factory-transcripts}"
mkdir -p "$DEST"
GCLOUD="$(command -v gcloud || echo /usr/local/bin/gcloud)"
echo "factory sync: $TOKEN_BURN_FACTORY_BUCKET -> $DEST ($(date '+%F %T'))"
"$GCLOUD" storage rsync -r "$TOKEN_BURN_FACTORY_BUCKET" "$DEST" --quiet \
  || echo "  WARNING: factory sync failed; snapshot uses the last mirrored copy"
