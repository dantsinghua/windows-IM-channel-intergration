#!/bin/bash
# QTrade 首启 ②:docker load 预载镜像 → 复核 → 写 .imported → 建库 → 起 Agent
#
# 规格:docs/03 §2.7.3 ②(a)~(e)、§2.2.1(rootfs_contents / IMAGES_LOADED 判据)
#
# 判据(§2.7.3 末):`docker images` 含两镜像且 digest = manifest rootfs_contents;
#                   `systemctl is-active qtrade-agent` = active;
#                   `curl -sf http://127.0.0.1:17600/api/v1/system/health`。
#
# 🔴 幂等:整个脚本可以重跑(修复/续跑路径会重跑)。每一步都是「已完成即跳过」。
set -uo pipefail

IMAGES_DIR=/var/lib/qtrade/images
CONTENTS=/etc/qtrade/contents.json
IMPORTED=/etc/qtrade/.imported
LOADED_MARK="$IMAGES_DIR/.image-loaded"
ENV_FILE=/mnt/c/ProgramData/QTrade/install/install.env
AGENT_PY=/opt/qtrade/agent/venv/bin/python
AGENT_DB=/var/lib/qtrade/agent.db
LOG='logger -t qtrade-firstboot --'

log() { $LOG "$*"; echo "[firstboot] $*"; }
die() { $LOG "FATAL: $*"; echo "[firstboot] FATAL: $*" >&2; exit 1; }

# ── (a) docker load 预载镜像 ────────────────────────────────────────────────
# 先按 contents.json 复核 tar 的 sha256 再 load —— 反过来的话,一个被改过的镜像
# 已经进了本地镜像库,再发现不对就得先 rmi,而 rmi 会碰到「别的容器在用」的情形。
load_images() {
    if [ -f "$LOADED_MARK" ]; then
        log "镜像已加载过($LOADED_MARK),跳过"
        return 0
    fi
    [ -r "$CONTENTS" ] || die "缺 $CONTENTS(G-10 内容清单),无法复核镜像"

    local n=0
    for key in redroid_image napcat_image; do
        local tar ref want_sha
        tar="$(jq -r --arg k "$key" '.[$k].tar // empty' "$CONTENTS")"
        ref="$(jq -r --arg k "$key" '.[$k].ref // empty' "$CONTENTS")"
        want_sha="$(jq -r --arg k "$key" '.[$k].sha256 // empty' "$CONTENTS")"
        [ -n "$tar" ] && [ -n "$ref" ] || die "contents.json 里 $key 缺 tar/ref"
        [ -r "$tar" ] || die "预载镜像 tar 不存在:$tar"

        if [ -n "$want_sha" ]; then
            local got
            got="$(sha256sum "$tar" | cut -d' ' -f1)"
            # §2.2.1:不一致 → E_INSTALL_IMAGE_LOAD_FAILED(附不一致项)
            [ "$got" = "$want_sha" ] || die "E_INSTALL_IMAGE_LOAD_FAILED:$tar sha256 不符(期望 $want_sha,实得 $got)"
        fi

        log "docker load $ref ← $tar"
        docker load -i "$tar" >/dev/null || die "E_INSTALL_IMAGE_LOAD_FAILED:docker load 失败($tar)"

        docker image inspect "$ref" >/dev/null 2>&1 \
            || die "E_INSTALL_IMAGE_LOAD_FAILED:load 完仍找不到镜像 $ref"
        n=$((n + 1))
    done

    # digest 复核(§2.2.1「docker images --digests 比对」)。
    # ⚠️ `docker save` 出来的 tar 再 load 进来,RepoDigests 会是空的 —— digest 是
    # registry 侧的内容寻址,save/load 这条路上不经过 registry。所以这里只在
    # contents.json 真的记了 digest **且**本地能读出来时才比,读不出来记 warn 不阻断:
    # tar 的 sha256 已经把「文件有没有被换掉」这件事锁死了,那才是我们要防的。
    for key in redroid_image napcat_image; do
        local ref want_digest got_digest
        ref="$(jq -r --arg k "$key" '.[$k].ref // empty' "$CONTENTS")"
        want_digest="$(jq -r --arg k "$key" '.[$k].digest // empty' "$CONTENTS")"
        [ -n "$want_digest" ] || continue
        got_digest="$(docker image inspect "$ref" --format '{{index .RepoDigests 0}}' 2>/dev/null | cut -d@ -f2)"
        if [ -z "$got_digest" ]; then
            log "warn:$ref 本地无 RepoDigest(save/load 路径的正常现象),已由 tar sha256 复核代替"
        elif [ "$got_digest" != "$want_digest" ]; then
            die "E_INSTALL_IMAGE_LOAD_FAILED:$ref digest 不符(期望 $want_digest,实得 $got_digest)"
        fi
    done

    printf 'loaded_at=%s\ncount=%s\n' "$(date -Is)" "$n" > "$LOADED_MARK"
    log "预载镜像 $n 个加载完成"
}

# ── (e)→(d) 建库与起 Agent ──────────────────────────────────────────────────
# ⚠️ 规格 §2.7.3 ② 把这两步写成「(d) systemctl enable --now qtrade-agent;
#    (e) 第一次 agent --init-db 建 agent.db」。这里**把顺序对调**,理由:
#    照字面先 enable --now,Agent 会在 agent.db 还不存在时启动、自己开库跑迁移
#    (02 §2.1 第 4 步),而紧接着的 --init-db 就会撞上一个已被 Agent 持有
#    (WAL + 连接)的库;两边同时建表的结果不确定,而 02 §2.1 第 4 步明写
#    「迁移失败**拒绝启动**」——一次竞争就是一次起不来。
#    先 --init-db(幂等)再 enable --now,两条路径的终态完全一致、没有竞争窗口。
#    🔴 这是一处**规格顺序与实现顺序不一致**,要么按本注释收口成 §15g 裁决,
#       要么把 §2.7.3 ②(d)(e) 的次序调过来。交付时已列入待裁决项,未擅自改文档。
init_db_and_start_agent() {
    [ -x "$AGENT_PY" ] || die "Agent venv 不存在:$AGENT_PY"

    if [ -f "$AGENT_DB" ]; then
        log "agent.db 已存在,跳过 --init-db"
    else
        log "建 agent.db(02 §3.1 DDL)"
        "$AGENT_PY" -m qtrade_agent.main --init-db --db "$AGENT_DB" \
            || die "agent --init-db 失败"
    fi

    log "systemctl enable --now qtrade-agent"
    systemctl enable --now qtrade-agent.service || die "qtrade-agent 起不来"
}

# ── (b) 写 .imported ────────────────────────────────────────────────────────
# 🔴 R5-9:.imported 落在「选段 + daemon.json + dockerd 都就绪之后」,它是选段的
#    **后置结果**,绝不能反过来当选段的前置闸门(挂反了选段永远晚于 dockerd、
#    172.17 早被占走、选段白做)。所以这一步在最后。
write_imported() {
    local pkg_ver rootfs_ver
    pkg_ver='unknown'
    [ -r "$ENV_FILE" ] && pkg_ver="$(grep -E '^package_version=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r\n "')"
    [ -n "$pkg_ver" ] || pkg_ver='unknown'
    rootfs_ver="$(cat /etc/qtrade/rootfs-version 2>/dev/null || echo unknown)"

    jq -n --arg p "$pkg_ver" --arg r "$rootfs_ver" --arg t "$(date -Is)" \
        '{package_version:$p, rootfs_version:$r, imported_at:$t}' > "$IMPORTED"
    chmod 0644 "$IMPORTED"
    log ".imported 已写:package_version=$pkg_ver rootfs_version=$rootfs_ver"
}

main() {
    log "首启开始"
    # dockerd 刚被 systemd 拉起时,socket 可能还没准备好接客;等一小会儿再 load。
    for i in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        [ "$i" = 30 ] && die "E_INSTALL_DOCKER_NOT_READY:等了 30 次 dockerd 仍不可用"
        sleep 2
    done

    load_images            # (a)
    init_db_and_start_agent # (e)+(d),顺序理由见函数头
    write_imported         # (b)

    log "首启完成"
}

main "$@"
