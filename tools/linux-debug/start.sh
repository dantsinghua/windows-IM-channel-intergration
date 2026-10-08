#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
runtime_python=/workspace/.qtrade-linux-debug/venv/bin/python
if [ ! -x "$runtime_python" ]; then runtime_python=python3; fi
exec "$runtime_python" -m uvicorn server:app --host 127.0.0.1 --port "${QTRADE_DEBUG_PORT:-17620}" --ws websockets --ws-max-size 65536 --no-access-log
