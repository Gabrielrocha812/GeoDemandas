#!/usr/bin/env bash
set -euo pipefail
umask 077
backup_dir="${BACKUP_DIR:-/var/backups/geodemandas}"
retention_days="${BACKUP_RETENTION_DAYS:-30}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup_dir"
: "${DATABASE_URL:?DATABASE_URL ausente}"
database_url="${DATABASE_URL/postgresql+psycopg:/postgresql:}"
pg_dump --format=custom --file="$backup_dir/database-$stamp.dump" "$database_url"
tar --create --gzip --file="$backup_dir/uploads-$stamp.tar.gz" -C /var/lib/geodemandas uploads
sha256sum "$backup_dir/database-$stamp.dump" "$backup_dir/uploads-$stamp.tar.gz" > "$backup_dir/checksums-$stamp.sha256"
find "$backup_dir" -maxdepth 1 -type f -mtime "+$retention_days" -print -delete
