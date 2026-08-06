#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo "execute como root" >&2; exit 1; }
source_dir="$(cd "$(dirname "$0")/.." && pwd -P)"
release="$(date -u +%Y%m%dT%H%M%SZ)"
release_dir="/opt/geodemandas/releases/$release"
[[ -f /etc/geodemandas/geodemandas.env ]] || { echo "configure /etc/geodemandas/geodemandas.env" >&2; exit 1; }
install -d -m 0755 /opt/geodemandas/releases
install -d -m 0750 -o geodemandas -g geodemandas /var/lib/geodemandas/uploads
cp -a "$source_dir" "$release_dir"
chown -R root:root "$release_dir"
python3 -m venv /opt/geodemandas/venv
/opt/geodemandas/venv/bin/pip install --requirement "$release_dir/requirements.txt"
set -a; source /etc/geodemandas/geodemandas.env; set +a
(cd "$release_dir" && /opt/geodemandas/venv/bin/alembic upgrade head)
ln -sfn "$release_dir" /opt/geodemandas/current.next
mv -Tf /opt/geodemandas/current.next /opt/geodemandas/current
install -m 0644 "$release_dir/deploy/geodemandas-web.service" /etc/systemd/system/
install -m 0644 "$release_dir/deploy/geodemandas-workers.service" /etc/systemd/system/
install -m 0644 "$release_dir/deploy/geodemandas-backup.service" /etc/systemd/system/
install -m 0644 "$release_dir/deploy/geodemandas-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now geodemandas-workers geodemandas-web geodemandas-backup.timer
sleep 2
"$release_dir/deploy/healthcheck.sh"
