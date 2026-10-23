#!/bin/bash
# QTrade:容器停止即清可再生缓存 —— **事件监听与钩子分发**(E-19 ②)
#
# 规格:docs/03 §2.7.3 末条 ——「03 只保证目录与钩子随 rootfs 就位、单元 enable;
#       **具体清理逻辑归 02**(基线 §11.12 [MONITOR] ③ / 02 §2.8.8 _purge_ephemeral)」。
#
# 🔴 所以本脚本**不实现任何清理动作**,它只做两件事:
#      ① 监听 docker 的 die/stop 事件;
#      ② 按字典序依次执行 /etc/qtrade/hooks/on-container-stop.d/ 下的可执行钩子,
#         把容器名与 id 作为参数传进去。
#    真正清什么由 Agent(02)往那个目录里放钩子决定。把清理写死在这里就等于
#    把运行期策略钉死在安装器里,改一次策略要重出 rootfs。
set -uo pipefail

HOOK_DIR=/etc/qtrade/hooks/on-container-stop.d
LOG='logger -t qtrade-janitor --'

$LOG "启动,监听 docker die/stop 事件,钩子目录 $HOOK_DIR"

# --format 只取需要的两个字段,避免解析整个 JSON;
# `docker events` 在 dockerd 重启时会断开,单元的 Restart=always 负责把我们拉回来。
docker events --filter 'type=container' --filter 'event=die' --filter 'event=stop' \
              --format '{{.Actor.Attributes.name}} {{.Actor.ID}}' |
while read -r name id; do
    [ -n "$name" ] || continue
    # 只管我们自己的容器。别账号、别项目的容器停了不该触发我们的钩子。
    case "$name" in
        qtrade-*|qd[0-9]*|qq[0-9]*) ;;
        *) continue ;;
    esac
    $LOG "容器停止:$name ($id),分发钩子"
    [ -d "$HOOK_DIR" ] || continue
    for hook in "$HOOK_DIR"/*; do
        [ -x "$hook" ] || continue
        if ! "$hook" "$name" "$id"; then
            # 单个钩子失败不能让 janitor 退出 —— 它退出就再没人清下一个容器了。
            $LOG "钩子失败(已忽略):$hook $name"
        fi
    done
done
