#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 ]]; then echo "uso: restore.sh BANCO.dump UPLOADS.tar.gz" >&2; exit 2; fi
db_dump="$(realpath "$1")"
uploads_dump="$(realpath "$2")"
[[ -f "$db_dump" && -f "$uploads_dump" ]] || { echo "backup inexistente" >&2; exit 2; }
: "${DATABASE_URL:?DATABASE_URL ausente}"
database_url="${DATABASE_URL/postgresql+psycopg:/postgresql:}"
echo "A restauracao substituira os dados do ambiente configurado. Digite RESTAURAR:"
read -r confirmation
[[ "$confirmation" == "RESTAURAR" ]] || { echo "cancelado"; exit 1; }
systemctl stop geodemandas-web geodemandas-workers
pg_restore --clean --if-exists --no-owner --dbname="$database_url" "$db_dump"
install -d -m 0750 -o geodemandas -g geodemandas /var/lib/geodemandas
tar --extract --gzip --file="$uploads_dump" -C /var/lib/geodemandas
chown -R geodemandas:geodemandas /var/lib/geodemandas/uploads
systemctl start geodemandas-workers geodemandas-web
