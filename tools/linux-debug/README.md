# Linux debug：本机 redroid / 企点

这是独立调试入口，不依赖尚未实现的 Electron、WinAgent、Windows 或 WSL。
仅新增本目录，现有设计文档和对账器保持原样；也不复制生产侧消息入库、账号管理等业务逻辑。

```text
浏览器页面（17620）→ FastAPI → Docker exec / ADB
                                  ↓
本机 QEMU 软件虚拟机 → Debian + Binder → Docker → redroid 11 → 企点 APK
```

云宿主内核没有 Binder，不能直接运行 redroid。这里在同一云机器内运行具备
Binder 的 Linux 客体，不需要远程 redroid 主机。没有 `/dev/kvm`，使用 QEMU TCG
软件模拟，因此不能用本环境的响应时间推断真实部署性能。

## 启动

工作目录：`/workspace/windows-IM-channel-intergration/tools/linux-debug`。
现有基础环境已提供 Python、FastAPI、Uvicorn、Playwright 和 Chromium。
专用虚拟环境位于 `/workspace/.qtrade-linux-debug/venv`，启动脚本优先使用它。
依赖版本见 `requirements.txt` / `requirements-dev.txt`。如需重新准备：

```bash
python3 -m venv --system-site-packages /workspace/.qtrade-linux-debug/venv
/workspace/.qtrade-linux-debug/venv/bin/python -m pip install -r requirements-dev.txt
```

```bash
cd /workspace/windows-IM-channel-intergration/tools/linux-debug
python3 prepare-vm.py
bash vm-start.sh
python3 install-video.py
bash start.sh
```

页面监听本机 **17620**，支持连接设备、查看实际启动状态、打开企点、获取截图、
勾选后点击设备画面，以及主页/返回/退格/回车和 ASCII 输入。
实时模式通过 scrcpy/H.264/WebSocket/WebCodecs 显示设备，支持原生触控与拖动。
没有任意 shell、任意 SQL 或自动发送消息接口。输入内容不写入访问日志或磁盘。
云环境的本机端口不等于用户电脑的 localhost；本次 onboarding UI 不提供页面预览转发。
页面样式内嵌于 `web/index.html`，单文件预览也能展示布局；未连接后台时明确提示
“未连接调试服务”。预览不会自动获得设备控制能力。真实远程验证需要可用的 HTTPS
入口，同时转发页面和 `/api/debug/`，并配置访问认证。当前服务仅允许本机访问，
不能将其原样公开；普通静态 Sites 托管本身不提供 redroid 后台连接。

运行文件均在 `/workspace/.qtrade-linux-debug`：

- `vm/rootfs.img`：12 GiB 稀疏磁盘，内含 Android `/data` 及后续登录态。
- `vm/payload`：镜像归档、APK 等只读共享载荷。
- `vm/id_ed25519`、`vm/known_hosts`：专用客体 SSH 身份与固定主机公钥。
- `android`：专用 ADB 密钥；`artifacts`：浏览器检查截图。

`prepare-vm.py` 遇到已有磁盘会保留并检查，不会重建或清空登录态。
VM 分配 4 vCPU / 8 GiB 内存，外层容器内存上限 12 GiB。
镜像及虚拟磁盘会占数 GiB 空间，安装前确认至少约 12 GiB 可用。
首次安装通过 Debian 签名包索引安装 QEMU/内核/Docker；redroid 按摘要固定，
ADB 按 Google 官方 SDK 元数据的校验值验证，保持 TLS 校验。

## 设备就绪

```bash
bash vm-ssh.sh 'ls -l /dev/binder /dev/hwbinder /dev/vndbinder; systemctl status qtrade-redroid --no-pager'
docker exec qtrade-linux-debug-vm /opt/platform-tools/adb connect 127.0.0.1:15555
docker exec qtrade-linux-debug-vm /opt/platform-tools/adb -s 127.0.0.1:15555 shell getprop sys.boot_completed
```

最后一条必须输出 `1`。首次镜像导入和 Android 启动可能需要数分钟。
仅 Docker 容器处于 running 状态不代表 Android 就绪。
宿主 Docker 的端口映射在某些云隔离网络内不能从宿主 loopback 直达，
因此 SSH 和 ADB 均通过 `docker exec` 在 QEMU 容器内连接，SSH 主机公钥验证保持启用。

云环境的代理与 CA 不会自动传给 Android。Android 启动后运行：

```bash
python3 configure-network.py
```

该命令复用现有平台代理和公有 CA，先验证客体 HTTPS，再配置 Android 全局 HTTP 代理
及系统 CA 信任库；保留 TLS 和域名白名单校验。恢复到新云机器后应重跑以更新代理地址。
应用若使用绕过系统 HTTP 代理的原生 TCP 通道，仍需另行验证平台是否支持该协议。

## 企点 APK

用户指定来源：
`https://dldir1.qq.com/qqfile/crm/qidian/qidian_android_6.9.9.10_release.apk`。

```bash
bash install-apk.sh
```

需要网络允许 `dldir1.qq.com`。安装脚本在下载成功、ZIP 完整性检查通过后才安装，
验证本次从指定 HTTPS 地址取得的 SHA-256：
`31d241ded078b4c26c029a7cd60bd9b7261ce6a9de2697d18bb6c49704aa42b3`。
Android 包管理器验证 APK 签名，不清理旧数据。
此版本未预设为与原机版本相同，需实际检查 ABI/native bridge、启动及登录。
真实账号登录后，截图和虚拟磁盘可能包含账号数据，不应提交到 Git 或公开分享。

## 验证

```bash
python3 -m unittest -v test_server.py
python3 e2e.py
python3 e2e.py --device
python3 e2e.py --qidian
python3 e2e_remote.py
```

- 单测：明确使用设备替身，验证边界、错误传播、参数校验及命令转义。
- 普通浏览器检查：实际启动页面，验证状态显示与不可用操作。
- `--device`：必须连接真实 Android 并经页面取得可渲染的 PNG；无设备会失败，不跳过。
- `--qidian`：还要求已安装真实 APK，点击打开后确认企点位于前台并取得截图。
  这证明应用能打开，不证明登录或消息收发成功。

完整企点业务 E2E 留待登录完成后：确认指定测试会话、发送约定文本、读取主库确认
`qd:{uniseq}`，以及接收去重等；本调试台不以固定坐标自动发送，不伪造主库读取结果。
原机 `xunjia-agent/relay/side_a` 未随此仓库提供，后续应复用该实现再接入业务验证。
UI 改动后重新跑浏览器检查；文档有变时照旧在 `docs/` 运行 `python3 check-truth-tables.py`。

本次安装确认版本名 `6.9.9` / 版本码 `6909`（用户指定文件名 `6.9.9.10`），
APK 仅包含 ARM64 原生库，已通过 redroid 的 `libndk_translation` 启动并进入登录页。
已按用户授权通过网页实时控制提交登录，出现“登录中”后回到登录页，未确认成功。
本次诊断中 Android 无法解析企点域名，宿主/客体直连 TCP 被拒绝，而宿主经平台代理
访问企点官网 HTTPS 返回 200；这些检查不等同于企点登录协议已连通，也不能据此判断凭据错误。
真实消息收发仍未验证，不能用进程启动替代。

## Sites 与远程访问

参见 [SITES.md](SITES.md)：前后端接口、准确来源的 CORS、WS 独立鉴权、TLS 入口、
静态部署包及实测边界。`python build-site.py` 生成不包含运行数据的前端包；
Sites 站点与公网入口尚未部署，不能将本机跨域 E2E 当作 Sites 验证通过。

## 诊断与保留

```bash
docker logs --tail 60 qtrade-linux-debug-vm
bash vm-ssh.sh 'journalctl -u qtrade-redroid -n 40 --no-pager; docker logs --tail 60 qtrade-redroid'
```

不要删除 `rootfs.img` 或执行 `pm clear` 来修复一般连接问题。
发布环境会保留文件，但服务进程不保证存活；新任务按上述启动步骤恢复。
如需重做虚拟磁盘，应先停止 VM 并备份磁盘，单独明确授权后再执行。
