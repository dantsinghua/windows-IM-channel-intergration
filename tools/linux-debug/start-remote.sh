#!/usr/bin/env bash
set -euo pipefail
: "${QTRADE_SITE_ORIGIN:?Set the actual Sites origin returned by deployment}"
: "${QTRADE_API_ORIGIN:?Set the actual public HTTPS API origin}"
: "${QTRADE_REMOTE_TOKEN_FILE:?Set a private credential-file path, mode 600}"
export QTRADE_DEBUG_PORT="${QTRADE_DEBUG_PORT:-17622}"
exec bash "$(dirname "$0")/start.sh"
