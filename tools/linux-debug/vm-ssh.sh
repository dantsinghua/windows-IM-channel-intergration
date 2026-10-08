#!/usr/bin/env bash
set -euo pipefail
exec docker exec qtrade-linux-debug-vm ssh -F /dev/null -i /data/id_ed25519 \
  -o UserKnownHostsFile=/data/known_hosts -o StrictHostKeyChecking=yes \
  -o BatchMode=yes -o ConnectTimeout=15 -p 10022 root@127.0.0.1 "$@"
