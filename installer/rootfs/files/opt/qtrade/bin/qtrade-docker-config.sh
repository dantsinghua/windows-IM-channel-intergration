#!/bin/bash
# Apply the target-machine network plan before dockerd starts.
# The installer injects this local file through stdin; no Windows drive is assumed.
set -euo pipefail

ENV_FILE=/etc/qtrade/install.env
DAEMON_JSON=/etc/docker/daemon.json
MARKER=/etc/qtrade/.docker-configured

/opt/qtrade/agent/venv/bin/python - "$ENV_FILE" "$DAEMON_JSON" "$MARKER" <<'PY'
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


try:
    env_path, daemon_path, marker = map(Path, sys.argv[1:])
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in {"QTRADE_DOCKER_POOL_BASE", "QTRADE_DOCKER_BIP"}:
            if key in values:
                raise ValueError("duplicate network key")
            values[key] = value
    pool = ipaddress.IPv4Network(values["QTRADE_DOCKER_POOL_BASE"], strict=True)
    bridge = ipaddress.IPv4Interface(values["QTRADE_DOCKER_BIP"])
    private = map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    if not any(pool.subnet_of(parent) for parent in private) or not 8 <= pool.prefixlen <= 24:
        raise ValueError("invalid private pool")
    if bridge.network.prefixlen != 24 or not bridge.network.subnet_of(pool):
        raise ValueError("bridge is outside pool")
    if bridge.ip in (bridge.network.network_address, bridge.network.broadcast_address):
        raise ValueError("invalid bridge host address")
    config = json.loads(daemon_path.read_text(encoding="utf-8")) if daemon_path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("invalid daemon configuration")
    wanted = {"default-address-pools": [{"base": str(pool), "size": 24}], "bip": str(bridge)}
    for key, value in wanted.items():
        if key in config and config[key] != value:
            raise ValueError("existing network configuration conflicts with install plan")
        config[key] = value
    config.setdefault("log-driver", "json-file")
    config.setdefault("log-opts", {"max-size": "20m", "max-file": "3"})
    atomic_write(daemon_path, json.dumps(config, indent=2) + "\n", 0o644)
    atomic_write(marker, f"base={pool}\nbip={bridge}\n", 0o644)
except (OSError, ValueError, KeyError, TypeError):
    print("QTrade Docker configuration is missing, invalid, or conflicts with existing settings.", file=sys.stderr)
    raise SystemExit(1)
PY
