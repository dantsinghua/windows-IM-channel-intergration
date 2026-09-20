#!/bin/bash
# QTrade 首启 ②:docker load 预载镜像 → 复核 →(d)建库 →(e)起 Agent →(b)写 .imported
#
# 规格:docs/03 §2.7.3 ②(a)~(e)、§2.2.1(rootfs_contents / IMAGES_LOADED 判据)
#       (d)(e) 的顺序由 00 §15g R6-60 (a) 裁决对调;(b) 在最后的理由见 write_imported 头(R5-9)。
#
# 判据(§2.7.3 末):`docker images` 含两镜像且 digest = manifest rootfs_contents;
#                   `systemctl is-active qtrade-agent` = active;
#                   `curl -sf http://127.0.0.1:17600/api/v1/system/health`。
#
# 🔴 幂等:整个脚本可以重跑(修复/续跑路径会重跑)。(a) 与 (b) 靠标记文件「已完成即跳过」;
#    🔴 但 (d) `--init-db` **不设跳过闸门** —— 它自身幂等,理由与后果见该函数头(R6-62 Ⅰ(i))。
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

# ── (d) 建库 →(e) 起 Agent ─────────────────────────────────────────────────
# 规格:docs/03 §2.7.3 ②(d)(e)。**顺序已由 00 §15g R6-60 (a) 裁决对调**:
#   (d) `agent --init-db` 建 / 迁移 `agent.db`;(e) `systemctl enable --now qtrade-agent`。
#   照对调前的字面先 `enable --now`,Agent 会在 agent.db 还不存在时启动、自己开库跑迁移
#   (02 §2.1 第 4 步),紧接着的 `--init-db` 就撞上一个已被 Agent 持有(WAL + 连接)的库;
#   而 02 §2.1 第 4 步明写「迁移失败拒绝启动」—— 一次竞争就是一次首装失败。
#   先建库再拉起,两条路径终态一致、没有竞争窗口。文档侧已按此收口,本函数就是那个实现。
#
# 🔴 R6-62 Ⅰ(i) / 03 §2.7.3 ②(d) 硬约束:**本步必须无条件调用 `--init-db`**,
#   调用方**不得自加「库文件在就不调」的前置闸门**。`--init-db` 自身幂等 —— 已是最新则
#   一条语句都不写、回 0;落后则按 `schema_version` 把迁移跑完。在外面加闸门 =
#   把 R6-61/W4 刚作废的「库已存在即跳过」语义换个地方写:**升级 / 修复路径上迁移不跑**,
#   且失败会退化成「Agent 起不来」、拿不到可诊断的 3/4/5(03 §8b.3 M1-12b ②③ 即因此恒红)。
#
# 退出码(owner = 02 §2.1「Agent 命令行参数」;3/4/5 只在本机首启日志里可见,
# 引擎侧统一表现为 `AGENT_NOT_READY` / 退出码 75,03 §5.1):
#   0 成功(含「已最新、什么都没改」的幂等路径)   2 argparse 用法错误
#   3 库损坏 / 不是 SQLite(quick_check 未过,不动该文件)
#   4 库 schema_version 高于本版代码上限(不动该文件;不支持降级)
#   5 其它:父目录建不出 / DDL 或迁移失败 / 磁盘满 / 权限不足(可能留半建库,下次续跑)
init_db_and_start_agent() {
    [ -x "$AGENT_PY" ] || die "Agent venv 不存在:$AGENT_PY"

    log "agent --init-db(无条件调用;幂等:已最新不写、落后则迁移)→ $AGENT_DB"
    local rc=0
    "$AGENT_PY" -m qtrade_agent.main --init-db --db "$AGENT_DB" || rc=$?
    case "$rc" in
        0) log "agent.db 已就绪(--init-db 退出码 0)" ;;
        3) die "agent --init-db 退出码 3:$AGENT_DB 损坏或不是 SQLite(quick_check 未过),该文件未被改动。带诊断包报障,别自行删库(02 §2.6:日志指向最近备份)" ;;
        4) die "agent --init-db 退出码 4:$AGENT_DB 的 schema_version 高于本版 Agent 代码上限,该文件未被改动;不支持降级(02 §3.8)。带诊断包报障" ;;
        5) die "agent --init-db 退出码 5:建库 / 迁移失败(父目录建不出、DDL 或迁移失败、磁盘满、权限不足之一)。清磁盘 / 修权限后重跑首启:下次会无条件再调一次 --init-db 续跑(03 §5.1)" ;;
        2) die "agent --init-db 退出码 2:argparse 用法错误 —— 本版 Agent 不认这些参数,首启脚本与 wheel 版本对不上" ;;
        *) die "agent --init-db 退出码 $rc:未登记的退出码(02 §2.1 只登记 0/2/3/4/5)" ;;
    esac

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
    init_db_and_start_agent # (d)+(e),顺序理由见函数头(R6-60 (a))
    write_imported         # (b)

    log "首启完成"
}

main "$@"
