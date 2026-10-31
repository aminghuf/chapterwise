#!/usr/bin/env bash
# One-time setup on the VPS, as root:  bash server-bootstrap.sh
# Creates /root/chapterwise with a .env holding generated secrets, and asks for the
# web password and the DeepSeek API key. Safe to re-run: existing values are kept.
set -euo pipefail

DIR=/root/chapterwise
mkdir -p "$DIR"/{data/postgres,backups}
ENV="$DIR/.env"
touch "$ENV"
chmod 600 "$ENV"

set_if_missing() {  # name value
  grep -q "^$1=" "$ENV" || echo "$1=$2" >> "$ENV"
}

set_if_missing CHAPTERWISE_PUBLIC_URL https://read.aminghuf.com
set_if_missing BOOKS_HOST_PATH /root/bookorbit/books
set_if_missing CHAPTERWISE_MODEL deepseek-flash
set_if_missing TZ Europe/Rome
set_if_missing POSTGRES_PASSWORD "$(openssl rand -hex 24)"
set_if_missing CHAPTERWISE_SECRET_KEY "$(openssl rand -hex 32)"

if ! grep -q '^CHAPTERWISE_ADMIN_PASSWORD=' "$ENV"; then
  read -rsp "Password for the web UI: " pw; echo
  [ -n "$pw" ] || { echo "Password can't be empty"; exit 1; }
  echo "CHAPTERWISE_ADMIN_PASSWORD=$pw" >> "$ENV"
fi
if ! grep -q '^DEEPSEEK_API_KEY=' "$ENV"; then
  read -rsp "DeepSeek API key (Enter to add later): " key; echo
  echo "DEEPSEEK_API_KEY=$key" >> "$ENV"
fi

[ -d /root/bookorbit/books ] || echo "Warning: /root/bookorbit/books doesn't exist; set BOOKS_HOST_PATH in $ENV"
echo "Ready: $ENV (edit it any time, then run ./cw.sh up -d)"
