#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
payload=/workspace/.qtrade-linux-debug/vm/payload
apk=qidian_android_6.9.9.10_release.apk
mkdir -p "$payload"
if [ ! -f "$payload/$apk" ]; then
  curl --fail --show-error --location --proto '=https' --tlsv1.2 --max-time 180 \
    https://dldir1.qq.com/qqfile/crm/qidian/qidian_android_6.9.9.10_release.apk \
    -o "$payload/$apk.part"
  python3 - "$payload/$apk.part" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    assert 'AndroidManifest.xml' in archive.namelist(), 'Not an Android APK'
    assert archive.testzip() is None, 'Damaged APK archive'
PY
  mv "$payload/$apk.part" "$payload/$apk"
fi
printf '%s  %s\n' '31d241ded078b4c26c029a7cd60bd9b7261ce6a9de2697d18bb6c49704aa42b3' "$payload/$apk" | sha256sum --check
docker exec qtrade-linux-debug-vm /opt/platform-tools/adb connect 127.0.0.1:15555
test "$(docker exec qtrade-linux-debug-vm /opt/platform-tools/adb -s 127.0.0.1:15555 shell getprop sys.boot_completed | tr -d '\r\n')" = 1
# Android's package manager verifies APK signatures; keep existing app data.
docker exec qtrade-linux-debug-vm /opt/platform-tools/adb -s 127.0.0.1:15555 install -r --no-streaming "/data/payload/$apk"
docker exec qtrade-linux-debug-vm /opt/platform-tools/adb -s 127.0.0.1:15555 shell pm path com.tencent.qidian
