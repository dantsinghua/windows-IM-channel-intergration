# 手动装机步骤:给 WSL2 换上带 binder 的自建内核

> 产物:`kernel/out/bzImage`(内核版本 `6.6.123.2-microsoft-standard-WSL2-binder`,基于 microsoft/WSL2-Linux-Kernel `linux-msft-wsl-6.6.y` @ a07f9ea8,官方 `Microsoft/config-wsl` + `kernel/binder.config` + `kernel/patches/*.patch`)。
> 文件已复制到 Windows 侧 `C:\Users\anlin\.qtrade-redroid\bzImage`。
> 大小 / SHA256:见文末「产物指纹」。
>
> ⚠️ 本文的一切动作都需要 `wsl --shutdown`,**会杀掉 WSL 里所有进程**(tmux、frpc 隧道、docker 容器、所有 Claude Code 会话)。请挑一个可以中断的时间点。
## 推荐:一键脚本(自动验证、失败自动回滚)——2026-09-16 晚补

在 **Windows** 上双击(不要在 WSL 里跑,shutdown 会杀掉自己):

- `C:\Users\anlin\.qtrade-redroid\apply-binder-kernel.cmd` —— 校验 bzImage sha256 → 备份 `.wslconfig` → 写 `kernel=` → `wsl --shutdown` → 120s 内验证 `uname -r` 带 `-binder`、binderfs 可挂载并出现 binder/hwbinder/vndbinder、docker 能起;**任一步失败自动恢复 `.wslconfig.bak-before-binder` 并再 shutdown 回到官方内核**。
- `C:\Users\anlin\.qtrade-redroid\rollback-kernel.cmd` —— 任何时候手动回滚。
- 预览不动手:`powershell -ExecutionPolicy Bypass -File C:\Users\anlin\.qtrade-redroid\apply-binder-kernel.ps1 -DryRun`

备份 `C:\Users\anlin\.wslconfig.bak-before-binder` 已于 2026-09-16 23:46 建好,与当前 `.wslconfig` 逐字相同。
即使新内核压根起不来(wsl 报错或挂起),`.wslconfig` 是 Windows 侧文件,回滚脚本不依赖 WSL,永远可执行。


## 0. 换内核前:先做这些

1. **tmux**:确认里面没有跑到一半的任务;需要的话 `tmux ls` 记下 session 名(重开 WSL 后 tmux 会话不会保留,只能重建)。
2. **frpc 隧道**(`~/.config/frp`,WSL 侧 frpc 进程):记下启动命令,重开后要手动拉起(Windows 侧的 frpc 服务不受影响)。
3. **Claude Code 会话**:让正在跑的会话收尾;它们会随 WSL 一起被杀。
4. **docker 容器**:`docker ps` 记一下,重开后 docker 守护进程会自动起,`--restart` 策略的容器会自动回来,其余手动 `docker start`。
5. 备份 `.wslconfig`:在 PowerShell 里
   ```powershell
   Copy-Item $env:USERPROFILE\.wslconfig $env:USERPROFILE\.wslconfig.bak-before-binder
   ```
6. 确认内核文件在位(PowerShell):
   ```powershell
   Get-Item $env:USERPROFILE\.qtrade-redroid\bzImage | Select-Object Length, LastWriteTime
   ```

## 1. 改 `.wslconfig`(只加一行)

文件:`C:\Users\anlin\.wslconfig`(用记事本 / VS Code 打开;从 WSL 里也可以 `nano /mnt/c/Users/anlin/.wslconfig`)。

在 **`[wsl2]` 段里**加入下面这**精确的一行**(反斜杠写两个,这是 .wslconfig 的转义规则;路径不要加引号):

```ini
kernel=C:\\Users\\anlin\\.qtrade-redroid\\bzImage
```

改完后 `[wsl2]` 段应长这样(其余行保持原样,别动):

```ini
[wsl2]
kernel=C:\\Users\\anlin\\.qtrade-redroid\\bzImage
memory=28GB
swap=16GB
networkingMode=NAT
firewall=false
dnsTunneling=false
autoProxy=false

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
```

**不需要** `kernelModules=`:docker / WSL 基础功能依赖的全部模块(lsmod 里的 26 个 + iptables/nftables/IPVS/网桥/tc/隧道/KVM/DM/NBD/常见文件系统/全部 crypto/Hyper-V,共 429 项)已直接编进内核(`=y`),换内核后不依赖 `/lib/modules` 目录。

> 备用路线(本次**不用**,仅记录):若以后需要用剩余的 `=m` 模块(真硬件驱动,Hyper-V 里用不到),可在源码目录 `make modules_install INSTALL_MOD_PATH=<目录>`,把 `<目录>/lib/modules/6.6.123.2-microsoft-standard-WSL2-binder` 打成 ext4/vhdx,再在 `[wsl2]` 里加 `kernelModules=C:\\path\\to\\modules.vhdx`(WSL ≥ 2.4.4 支持)。

## 2. 关闭 WSL(会杀所有会话)

PowerShell(普通权限即可):

```powershell
wsl --shutdown
```

等 8~10 秒(WSL 有个 8 秒的空闲回收窗口)。

## 3. 重开 WSL 并验证

开一个新的 Ubuntu 终端(或 PowerShell 里 `wsl`),逐条跑:

```bash
# 1) 内核版本:必须带 -binder 后缀
uname -r
#   期望:6.6.123.2-microsoft-standard-WSL2-binder

# 2) binder 设备节点(不会预先存在,见下方说明)
ls -l /dev/binder /dev/hwbinder /dev/vndbinder
#   ⚠️ 2026-09-16 QEMU 实测:6.x 内核开了 CONFIG_ANDROID_BINDERFS 后**不会**预先创建这三个字符设备,
#      这里报 No such file 是正常的;设备只在挂 binderfs 时出现(下一条),redroid 自己会挂并 symlink /dev/binder。
#      真正的判据是 /proc/filesystems 有 binder + 下一条 binderfs 试挂:
grep binder /proc/filesystems

# 3) 内核配置自证
zcat /proc/config.gz | grep -E 'ANDROID_BINDER|BINDERFS|DMABUF_HEAPS'
#   期望:CONFIG_ANDROID_BINDER_IPC=y / CONFIG_ANDROID_BINDERFS=y /
#        CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder" / CONFIG_DMABUF_HEAPS=y

# 4) binderfs 可挂(redroid 会用)
sudo mkdir -p /dev/binderfs && sudo mount -t binder binder /dev/binderfs && ls /dev/binderfs && sudo umount /dev/binderfs

# 5) docker 没退化
docker info --format '{{.ServerVersion}} {{.Driver}} {{.CgroupDriver}}'   # 期望 29.5.3 overlayfs ...
docker run --rm alpine sh -c 'ip a && wget -qO- http://1.1.1.1 >/dev/null && echo NET_OK'
sudo iptables -S | wc -l          # 应有几十条 docker 规则
sudo nft list tables              # 应能列出 ip nat / ip filter 等

# 6) 模块:内建之后 lsmod 基本为空是正常的,看内建清单
lsmod                              # 期望:几乎空
grep -c . /lib/modules/$(uname -r)/modules.builtin 2>/dev/null || echo "(无 modules 目录也正常)"
zcat /proc/config.gz | grep -E '^CONFIG_(BRIDGE|BRIDGE_NETFILTER|NFT_COMPAT|IP_NF_IPTABLES|TUN|KVM|TLS|AUTOFS_FS|CONFIGFS_FS)=y'
#   以上应全为 =y

# 7) WSL 基础功能
ls /mnt/c | head -3               # drvfs 挂载
ip a show eth0 && ping -c1 8.8.8.8   # 网络
cat /proc/cmdline                 # 应能看到 initrd=\initrd.img ...
```

重开后手动恢复:
```bash
tmux new -s main            # 重建 tmux
# 拉起 WSL 侧 frpc(按你原来的启动方式,例如:)
nohup ~/.local/bin/frpc -c ~/.config/frp/frpc.toml > ~/.config/frp/frpc.log 2>&1 &
docker ps -a                # 手动 start 没有 --restart 策略的容器
```

## 4. 回滚(内核起不来 / docker 坏了 / 想换回官方内核)

1. 编辑 `C:\Users\anlin\.wslconfig`,**删掉** `kernel=C:\\Users\\anlin\\.qtrade-redroid\\bzImage` 这一行(或前面加 `#` 注释掉)。
   —— 或者直接用备份覆盖:`Copy-Item $env:USERPROFILE\.wslconfig.bak-before-binder $env:USERPROFILE\.wslconfig -Force`
2. PowerShell:`wsl --shutdown`
3. 重开 WSL,`uname -r` 应回到 `6.6.87.2-microsoft-standard-WSL2`。

> WSL 完全起不来时(极少见):`.wslconfig` 是 Windows 侧文件,在 Windows 资源管理器里改就行,不依赖 WSL 能否启动。

## 5. 常见坑

- **`kernel=` 路径写错** → WSL 报 `The system cannot find the file specified` / 错误码 `0x80070002`,按回滚步骤删行即可。
- **路径里有单反斜杠** → .wslconfig 里 `\U` 之类会被当转义,必须写 `\\`。
- **`kernel=` 是全局项**,对该 Windows 用户下所有 WSL2 发行版生效(docker-desktop 等也会用这个内核)。
- **lsmod 为空不是故障**:功能都编进内核了,靠 `zcat /proc/config.gz` 看 `=y` 判断。
- **Windows 更新 WSL 后** `.wslconfig` 的 `kernel=` 仍然生效,不会被覆盖;但若 WSL 大版本升级要求新内核 ABI(极少),回滚即可。

## 切换前已做的启动验证(2026-09-17 00:05,QEMU/KVM,同一 bzImage)
- 内核在 QEMU 里启动到用户态并正常关机,无 panic/Oops;`uname -r` = `6.6.123.2-microsoft-standard-WSL2-binder`。
- binderfs 挂载成功,`binder/hwbinder/vndbinder` 三设备 + `BINDER_CTL_ADD` 新建设备,`BINDER_VERSION` ioctl 全部返回 protocol=8。
- docker 依赖:overlay、cgroup2 实际挂载成功,tun 设备存在,bridge/veth 创建成功,br_netfilter 与 conntrack 就位。
- 官方 6.6.87.2 内核用同一套测试作对照:binder 全 ENOENT(证明测试方法有效)。
- 第一版内核 tcrypt 开机自测刷 75 条失败(CRYPTO_TEST 误编入),已关掉重编,本版 dmesg 零 tcrypt。
- QEMU 测不到的:WSL 自己的 init、hv_vsock、dxgkrnl、9p drvfs、`.wslconfig` 切换流程——这部分靠一键脚本的自动回滚兜底。
- 证据文件:`~/.claude/jobs/5986b5d0/tmp/boottest/boot2.log`(新内核)、`boot.log`(第一版)、`boot-official.log`(官方对照)。

## 内核自带的崩溃转储防护(L2,2026-09-18 起)

**安琳拍板**:「core_pattern 这个属性要**封装进 exe 里的 wsl 内核**,而不是改我本地的 wsl2。」⇒ 本内核**自带**这项防护,装机与运行期**都不会改用户的任何 sysctl 配置**;换回官方内核即完全还原、零残留。

**要解决的问题**:WSL 的用户态 `/init` 启动时把 `core_pattern` 设成 `|/wsl-capture-crash %t %E %p %s`。该捕获器**绕过 `RLIMIT_CORE`**——即使容器已设 `ulimits: core: 0`,它照样把整个进程内存写成转储。实测:redroid 里企点(arm64,经 `libnb.so` 翻译层)的 `:video` 子进程**每小时**崩一次,单个转储 **16 GB**,一天 11 个 / **147 GB**,把 C 盘从 153 GB 吃到 5.8 GB,而企点主进程完全正常、用户无感。

🔴 **关键查证(改内核编译期默认值是无效的,别白编一版)**:`fs/coredump.c:69` 的默认值是上游原值 `"core"`,**微软没有 patch 它**;内核源码树里 grep `wsl-capture-crash` **零命中**。管道模式是**用户态 `/init` 在内核 initcall 之后**写进 sysctl 的,所以改默认值、加 `late_initcall` 都会被它覆盖。**必须在 sysctl 写入路径上拦。**

**实现**:`kernel/patches/0001-qtrade-reject-pipe-core-pattern.patch` —— 在 `core_pattern` 的 sysctl handler `proc_dostring_coredump()` 里,写入后若首字符是 `|` 就**静默降级**为文件模式 `/var/lib/qtrade/cores/core.%e.%p`。
- **刻意不返回错误**:返回错误可能让 WSL init 启动报错甚至中断;静默降级既保住了容器侧 `ulimits: core: 0`,又不影响用户自己用文件模式 core dump 调试。
- 首次拦截时打一条 `pr_warn_once("qtrade: pipe core_pattern rejected, forced to ...")`,`dmesg` 可查。
- **另一备选**(未采用):`CONFIG_COREDUMP=n` —— 更彻底(sysctl 节点都不存在),但整个 WSL 里任何进程都不能 core dump,连带关掉 `CONFIG_ELF_CORE`,代价过大。

**验证装机后是否生效**:
```bash
cat /proc/sys/kernel/core_pattern     # 期望:/var/lib/qtrade/cores/core.%e.%p(不是 |/wsl-capture-crash)
dmesg | grep 'qtrade: pipe core_pattern'   # 期望:有一条 warn,说明拦截确实发生过
```
⚠️ **L2 单独不够**,容器侧的 `ulimits: core: 0`(L1)与 `.wslconfig` 的 `maxCrashDumpCount`/`crashDumpFolder`(L3)仍然必须有。反面结论:**只设容器 `ulimit core=0` 完全无效**——实测对照组产出 1 个转储、实验组仍产出 1 个。三层缺一不可,口径见 `docs/design/00-共享基线与口径.md` §11.11 [DISK] ④。

## 产物指纹


- 文件:`kernel/out/bzImage`;⚠️ **2026-09-18 起仓库产物与 Windows 侧那份不再相同**,见下。
- **v4(2026-09-18 19:55,已编译校验、含崩溃转储 L2,`kernel/out/bzImage`)**:大小 22,192,128 字节;SHA256 `35a985bc223ff71d8bd0bee181a0325597bc265b09d16110f1e2ec59dec55662`。相对 v3 只多了 `patches/0001-qtrade-reject-pipe-core-pattern.patch`(见上一节)。**尚未投放到 Windows 侧、尚未真机切换**——刻意的:覆盖 `C:\Users\anlin\.qtrade-redroid\bzImage` 会让用户下次重启 WSL 时**在不知情的情况下**换上新内核,违反「内核应用的时机必须由用户决定」这条红线(00 §11.6 [NOSHUTDOWN])。投放 = 复制 bzImage 到该路径 **并**同步更新 `apply-binder-kernel.ps1` 里的 `$Expected`,两者必须一起做,否则脚本 sha256 校验必失败。
- **现役 v3(2026-09-17 20:46,真机切换通过,当前 `C:\Users\anlin\.qtrade-redroid\bzImage` 与运行中的内核)**:大小 22,192,128 字节;SHA256 `82f3981d7a8d94139bbfb1ccdcb6791ac171ae5f04232073db44d712b5c8367c`;`uname -r` = `6.6.123.2-microsoft-standard-WSL2-binder+`(带 `+`,是 v3 构建时的 LOCALVERSION 处理所致,判据用「含 `-binder`」而不是逐字相等)。**v3 不含 L2 防护**,所以在切到 v4 之前,崩溃转储仍需靠临时 `sysctl` 或 `.wslconfig maxCrashDumpCount` 兜着。
- 历史:v2(09-17 00:00,22,196,224 B,`814dac67…`)QEMU 全绿但**真机切换后 VM 直接关机**(`CONFIG_VIRTIO_VSOCKETS=y` 抢占 vsock 槽位致 hv_sock 注册失败);v1(09-16 17:48,`b121bd59…`)QEMU 冒烟抓出 tcrypt 噪声。v2/v3 差异与坑清单见 `docs/design/03-安装引导与自动化配置.md` §2.6.7。
- 编译环境:Ubuntu 24.04 / gcc 13.3.0 / pahole 1.25 / `make -j24 LOCALVERSION=`,首编 371 秒;09-17 关 CRYPTO_TEST 增量重编;v3 关 VIRTIO_VSOCKETS 重编;**v4(09-18)打 core_pattern patch 增量重编,`make fs/coredump.o` 零警告,`strings fs/coredump.o` 校验防护字符串命中**
- ⚠️ 本段以 `sha256sum kernel/out/bzImage` 与 CI manifest 为准,手写指纹会过期(2026-09-18 勘误:原写 v2)
- 完整 .config 备份:`kernel/out/config-wsl-binder`
- 校验(装机后 `sha256sum` 对得上就是这一份):
  ```powershell
  Get-FileHash $env:USERPROFILE\.qtrade-redroid\bzImage -Algorithm SHA256
  ```
