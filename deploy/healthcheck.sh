#!/usr/bin/env bash
set -euo pipefail
url="${GEODEMANDAS_HEALTH_URL:-${APP_BASE_URL:-https://127.0.0.1}/health}"
body="$(curl --insecure --fail --silent --show-error --max-time 10 "$url")"
python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["status"] == "ok" and not p["dev_mode"]' <<<"$body"
