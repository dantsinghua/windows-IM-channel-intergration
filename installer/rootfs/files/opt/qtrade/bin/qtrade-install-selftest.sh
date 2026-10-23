#!/bin/bash
# Installer-only smoke test. No account storage, login state or ADB commands.
set -euo pipefail
CONTENTS=/etc/qtrade/contents.json
BOOT_TIMEOUT="${1:-180}"
[[ "$BOOT_TIMEOUT" =~ ^[0-9]+$ ]] && (( BOOT_TIMEOUT > 0 && BOOT_TIMEOUT <= 600 )) || exit 2
exec 9>/run/qtrade-install-selftest.lock
flock -n 9 || { echo 'Selftest already running' >&2; exit 1; }
ref="$(jq -er '.redroid_image.ref | select(type == "string" and length > 0)' "$CONTENTS")"
timeout 15 docker info >/dev/null
if timeout 15 docker container inspect qtrade-selftest >/dev/null 2>&1; then
    echo 'Existing qtrade-selftest is not owned by this run' >&2
    exit 1
fi
run_id="$(cat /proc/sys/kernel/random/uuid)"
cid=''
cleanup() {
    local original=$?
    trap - EXIT
    if [ -n "$cid" ]; then
        local owner
        owner="$(timeout 15 docker container inspect --format '{{index .Config.Labels "io.qtrade.install-selftest"}}' "$cid")" || exit 1
        [ "$owner" = "$run_id" ] || { echo 'Container ownership changed' >&2; exit 1; }
        timeout 30 docker rm -f -v "$cid" >/dev/null || exit 1
    fi
    exit "$original"
}
trap cleanup EXIT
cid="$(timeout 60 docker create --pull=never --name qtrade-selftest --privileged \
    --label "io.qtrade.install-selftest=$run_id" --ulimit core=0 --memory 1024m \
    -p 127.0.0.1:16099:5555 "$ref" androidboot.redroid_width=720 androidboot.redroid_height=1280)"
[[ "$cid" =~ ^[0-9a-f]{64}$ ]] || { cid=''; echo 'Invalid created container identity' >&2; exit 1; }
start_ms="$(date +%s%3N)"
timeout 30 docker start "$cid" >/dev/null
deadline=$((SECONDS + BOOT_TIMEOUT))
while (( SECONDS < deadline )); do
    boot="$(timeout 10 docker exec "$cid" getprop sys.boot_completed)" || exit 1
    if [ "$(printf '%s' "$boot" | tr -d '\r\n')" = 1 ]; then
        elapsed=$(( $(date +%s%3N) - start_ms ))
        # Prove deletion before reporting success. The trap retries only our ID.
        owner="$(timeout 15 docker container inspect --format '{{index .Config.Labels "io.qtrade.install-selftest"}}' "$cid")"
        [ "$owner" = "$run_id" ] || exit 1
        timeout 30 docker rm -f -v "$cid" >/dev/null
        cid=''
        printf '{"ok":true,"redroid_boot_ms":%s}\n' "$elapsed"
        exit 0
    fi
    sleep 1
done
echo 'Redroid boot timed out' >&2
exit 1
