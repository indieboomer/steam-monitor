#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

git pull --rebase
docker-compose up -d
docker logs --tail 50 steam-monitor
