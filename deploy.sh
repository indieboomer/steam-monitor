#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

git pull --rebase
docker-compose down
docker rm -f steam-monitor 2>/dev/null || true
docker-compose up -d
docker logs --tail 50 steam-monitor
