#!/usr/bin/env bash
# docker compose wrapper on the server, so .env and the pinned image are always used.
#   ./cw.sh ps | ./cw.sh logs -f worker | ./cw.sh up -d | ./cw.sh backup [label]
set -euo pipefail
cd "$(dirname "$0")"

compose() {
  docker compose --env-file .env --env-file image.env -f docker-compose.yml "$@"
}

if [ "${1:-}" = "backup" ]; then
  # Database dump into ./backups, keeping the newest 7. Skipped when the database isn't running yet.
  if ! compose ps --status running --services 2>/dev/null | grep -qx db; then
    echo "Database not running; nothing to back up."
    exit 0
  fi
  mkdir -p backups
  out="backups/chapterwise-$(date +%Y%m%d-%H%M%S)${2:+-$2}.sql.gz"
  compose exec -T db pg_dump -U chapterwise -d chapterwise | gzip > "$out"
  ls -1t backups/*.sql.gz | tail -n +8 | xargs -r rm --
  echo "Backup written: $out"
  exit 0
fi

compose "$@"
