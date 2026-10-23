"""``%ProgramData%\\QTrade\\winagent\\winagent.toml`` 的默认值。

**规格唯一出处**:02 §7.2(本文件的骨架与键名以它为准)+ 04 §7(``[monitor]``/``[net]``/``[probe]``/``[wsl]``/``[wechat]``
这几段的**值 owner** 是 04,02 §7.2 只登记键名,两处同名同默认)+ 05 §7 的 ``winagent.toml`` 段(``[vault]``/``[wechat]``
里 05 独有的那几个键)。**新增键先进文档、再加到这里**;值必须与文档逐字相同。

🔴 单一出处的几条硬约定(照抄裁决):
- ``[wsl] memory_by_physical`` / ``memory_by_physical_wechat_on`` 的 **owner 是 04 §7**(R3-23:02 §7.2 不复述数组、只引用 04);
  这里取 04 的值,**改只能改 04**。
- ``[monitor] raw_retention_h / m1_retention_d / h1_retention_d`` **两库逐档必须相等**(R3-24),与 ``agent.toml [monitor]`` 同值。
- ``[wechat] enabled`` 是**微信模块开关的全系统唯一真值**(C-43);Agent 侧 ``resource_pools.wechat_enabled`` 只是快照。
- ``[wsl] never_shutdown`` 只读常量,写着提醒(00 §11.6 [NOSHUTDOWN])。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

PROGRAMDATA = "%ProgramData%\\QTrade"


@dataclass(frozen=True)
class ApiConfig:
    """02 §7.2 ``[api]``。监听集合 = ``{127.0.0.1} ∪ {vEthernet (WSL) 当前 IPv4}``,**不绑 0.0.0.0**(00 §3 / 04 §2.6.3)。"""
    bind_loopback: bool = True
    bind_wsl_adapter: bool = True
    port: int = 17610                                                   # 00 §3
    agent_token_ref: str = "vault://winagent/agent_token"               # C-04
    console_token_ref: str = "vault://winagent/console_token"
    pipe_console: str = r"\\.\pipe\qtrade-winagent"                     # 控制台令牌管道(C-04)
    pipe_user: str = r"\\.\pipe\qtrade-winagent-user"                   # 会话代理内部管道(§2.4.1;R3-12 无 -<SID> 后缀)
    api_version: str = "1.0"                                            # 只读;§3.8


@dataclass(frozen=True)
class IpcConfig:
    """02 §7.2 ``[ipc]`` / §2.4.1。"""
    heartbeat_s: int = 5
    offline_after_s: int = 15
    max_frame_kb: int = 1024


@dataclass(frozen=True)
class VaultConfig:
    """02 §7.2 ``[vault]`` + 05 §7 独有键(``entropy_file``/``max_value_bytes``/``audit_reads``)。"""
    dir: str = PROGRAMDATA + "\\winagent\\vault"
    dpapi_scope: str = "machine"                                        # machine|user;服务账号下必须 machine(R3-32 键名)
    entropy_file: str = "entropy.bin"
    max_value_bytes: int = 4096                                         # 05 §2.2.2:值上限 4 KB
    audit_reads: bool = True


@dataclass(frozen=True)
class MonitorConfig:
    """02 §7.2 ``[monitor]``(键名)+ 04 §7 ``[monitor]``(值 owner)。"""
    sample_interval_s: int = 10
    slow_interval_s: int = 60
    raw_retention_h: int = 24                                           # R3-24:两库逐档相等
    m1_retention_d: int = 7
    h1_retention_d: int = 30                                            # R5-13:90→30
    disk_warn_pct: int = 10
    disk_warn_mb: int = 5120                                            # E-18 三级水位 warn
    disk_high_mb: int = 2048                                            # high
    disk_crit_pct: int = 3
    disk_critical_mb: int = 1024                                        # critical
    disk_days_left_warn: int = 7
    wechat_disk_warn_mb: int = 2048                                     # R4-9:微信数据根独立 warn,不进产品级水位
    vhdx_growth_warn_gb: int = 20                                       # H23
    crash_dump_dir_warn_gb: int = 5                                     # R-10 配套
    guest_crash_min_per_hour: int = 1                                   # H26(R2-11)
    guest_crash_sustain_hours: int = 2
    mem_warn_mb: int = 2048                                             # E-19,与磁盘水位独立
    mem_critical_mb: int = 1024
    auto_stop_on_pressure: bool = False
    pending_reboot_check_min: int = 10                                  # H14


@dataclass(frozen=True)
class AlertConfig:
    """02 §7.2 ``[alert]`` / 04 §2.4.2。"""
    crit_repeat_min: int = 30
    warn_repeat_h: int = 6
    storm_per_min: int = 20                                             # 超出合并为 ALERT_STORM
    buffer_max: int = 1000                                              # Agent 不在时本地缓冲条数(§2.4 monitor)


@dataclass(frozen=True)
class NetConfig:
    """02 §7.2 ``[net]``(键名)+ 04 §7 ``[net]``(值 owner)。"""
    listen_loopback: str = "127.0.0.1"
    listen_wsl_adapter: bool = True
    firewall_rule_winagent: str = "QTrade-WinAgent-17610-from-WSL"      # 04 §2.6.3 固定规则名
    firewall_rule_agent_lan: str = "QTrade-Agent-17600-LAN"
    wsl_subnet_fallback: str = "172.16.0.0/12"
    vpn_adapter_patterns: tuple[str, ...] = (
        "Cisco AnyConnect", "Fortinet", "FortiClient", "Sangfor", "CorpLink", "Feilian", "飞连",
        "Pulse Secure", "Ivanti", "GlobalProtect", "Check Point", "TAP-Windows", "OpenVPN",
        "Array Networks", "EasyConnect")
    net_state_interval_s: int = 60
    reachability_probe: tuple[str, ...] = ("msfxg.3g.qq.com:443", "long.weixin.qq.com:443")
    reachability_timeout_s: int = 3


@dataclass(frozen=True)
class ProbeConfig:
    """02 §7.2 ``[probe]``(段名 ``[probe]``,C-43;原 ``[netprobe]`` 作废)+ 04 §7。"""
    dns_timeout_s: int = 3
    tcp_timeout_s: int = 5
    tls_timeout_s: int = 5
    http_timeout_s: int = 5
    periodic_interval_min: int = 10                                     # 0 = 只手动
    results_retention_d: int = 30
    wechat_hosts: tuple[str, ...] = ("long.weixin.qq.com:443", "long.weixin.qq.com:8080", "long.weixin.qq.com:80",
                                     "short.weixin.qq.com:443", "short.weixin.qq.com:80", "dns.weixin.qq.com:443")
    sample_duration_s: int = 10                                         # C-1 实测采样窗口,上限 30
    sample_after_wizard_s: int = 60
    single_target_total_s: int = 15                                     # 04 §2.8.1:单目标总上限 15s
    round_total_s: int = 20                                             # 04 §2.8.1:整轮 ≤20s


@dataclass(frozen=True)
class WslConfig:
    """02 §7.2 ``[wsl]``(段名 ``[wsl]``,C-43;03 的 ``[wslctl]`` 作废)+ 04 §7 ``[wsl]``(分档表 owner,R3-23)。"""
    distro: str = "qtrade"
    autostart: bool = True                                              # 会话代理上线时拉起发行版(C-02:随用户登录,不随开机)
    memory_by_physical: tuple[tuple[int, int], ...] = ((8, 5), (12, 8), (16, 11), (24, 16), (32, 24))
    memory_by_physical_wechat_on: tuple[tuple[int, int], ...] = ((8, 5), (12, 8), (16, 10), (24, 16), (32, 24))
    swap_gb: int = 2
    processors: int = 0                                                 # 0 = 不写(全部核)
    auto_memory_reclaim: str = "gradual"
    sparse_vhd: bool = True
    localhost_forwarding: bool = True
    gui_applications: bool = False
    backup_dir: str = PROGRAMDATA + "\\wsl"
    backup_keep: int = 10
    overwrite_existing: bool = True                                     # B-5:用户已有 memory= 也按分档表改
    never_shutdown: bool = True                                         # 🔴 只读常量(00 §11.6 [NOSHUTDOWN])
    kernel_dir: str = PROGRAMDATA + "\\kernel"                          # 03:内核文件落盘目录(服务的提权半)


@dataclass(frozen=True)
class WechatConfig:
    """02 §7.2 ``[wechat]``(配置文件的家在 02,R3-37)+ 04 §7(keep-awake / hosts 屏蔽的**行为 owner**)+ 05 §7(05 独有键)。"""
    enabled: bool = False                                               # 🔴 全系统唯一真值(C-43)
    chatlog_dir: str = PROGRAMDATA + "\\pkg\\chatlog"                   # 目录而非 exe(DLL 复制需要目录)
    # R6-90:chatlog 只认 `<cwd>/lib/windows_x64/wx_key.dll`;会话代理是普通用户、pkg 只读 ⇒ 选中的 DLL 落到用户可写目录作 cwd
    chatlog_work_dir: str = "%LOCALAPPDATA%\\QTrade\\chatlog"
    chatlog_port: int = 5030
    wxkey_dlls: tuple[str, ...] = ("wx_key2.dll", "wx_key1.dll")        # 试钥顺序(C-43 采 05;矩阵有 verified 时优先该 DLL)
    poll_interval_s: int = 5
    lookback_overlap_s: int = 1
    page_limit: int = 200
    confirm_poll_interval_ms: int = 1000                                # 发送后加速轮询
    confirm_timeout_ms: int = 10000                                     # 微信 10s 读回确认
    # R6-87:默认值 = 安装器实际落包的位置(03 §2.9.3 / collect-payload `pkg/wechat/weixin_4.1.12.26.exe`);
    # 此前写 `pkg\WeChatSetup.exe`,安装器又从不写本键 ⇒ 全新机器装完「重装」必缺文件。
    bundled_installer: str = PROGRAMDATA + "\\pkg\\wechat\\weixin_4.1.12.26.exe"
    bundled_version: str = "4.1.12.26"                                  # 随包微信版本(B-1;03 §2.9.3 钉死)
    # 🔴 R2-6:另一候选包 WeChatWin_4.1.12.exe 外层 VersionInfo 与之完全相同、装出来却是 4.1.12.55 —— 只能靠 sha256 分辨。
    # 空串 = 不校验(仅限明确知道自己在干什么的测试环境)。
    bundled_sha256: str = "58997cfe4513ab71f107c2137bb570ade030f228115c14688544cec80e604053"
    narrator_min_seconds: int = 300                                     # 讲述人仪式保底时长(C-43;R6-90 起为上限兜底)
    narrator_probe_min_seconds: int = 60                                # R6-90:已登录且可见时,满此值即提前试一次「关讲述人→复探」
    keep_awake_mode: str = "powercfg"                                   # request|powercfg|off(默认 powercfg,C-6)
    update_check_min: int = 10                                          # H20
    # ---- 04 §7 [wechat](行为 owner=04)
    powercfg_items: tuple[str, ...] = ("standby-timeout-ac", "standby-timeout-dc", "monitor-timeout-ac",
                                       "monitor-timeout-dc", "hibernate-timeout-ac", "hibernate-timeout-dc")
    update_block_hosts: bool = True                                     # B-2
    update_block_domains: tuple[str, ...] = ("dldir1.qq.com", "dldir1v6.qq.com")   # 🔴 只这 2 个下载 CDN(验证报告 §5.3)
    update_block_marker: str = "# QTrade-wechat-update-block"           # 🔴 R6-15:逐行行尾标记,不是 BEGIN/END 围栏
    update_block_check_min: int = 10                                    # H21
    hosts_backup_dir: str = PROGRAMDATA + "\\wechat"
    hosts_backup_keep: int = 5
    # ---- 05 §7 winagent.toml [wechat] 独有键
    exe_path: str = ""                                                  # 空 = 按注册表自动定位
    key_retry_per_hour: int = 3
    narrator_ritual: str = "auto"                                       # auto(先探可见性)| always | skip
    narrator_max_rounds: int = 2
    narrator_mute: bool = True
    logout_mode: str = "process"                                        # process | ui(D-1:默认结束进程)
    process_close_grace_s: int = 10                                     # 先 WM_CLOSE,10s 未退再 taskkill /F
    main_wnd_class: str = "Qt51514QWindowIcon"                          # 05 §2.4.9 记档默认(3.x 是 WeChatMainWndForPC)
    update_dir: str = "%APPDATA%\\Tencent\\xwechat\\update"             # 05 §2.4.9;04 H20 读此项


@dataclass(frozen=True)
class RetentionConfig:
    """02 §7.2 ``[retention]``(E-18 由 365→30,上限 30)。"""
    wa_audit_days: int = 30


@dataclass(frozen=True)
class LogConfig:
    """02 §7.2 ``[log]``;行格式 §2.9;会话代理日志 ``user-<sid>.log``。"""
    dir: str = "winagent\\logs"
    retention_days: int = 30
    level: str = "INFO"


@dataclass(frozen=True)
class WinAgentConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    ipc: IpcConfig = field(default_factory=IpcConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    net: NetConfig = field(default_factory=NetConfig)
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    wsl: WslConfig = field(default_factory=WslConfig)
    wechat: WechatConfig = field(default_factory=WechatConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    log: LogConfig = field(default_factory=LogConfig)

    def with_wechat(self, **kw: Any) -> "WinAgentConfig":
        return replace(self, wechat=replace(self.wechat, **kw))


_SECTIONS = {"api": ApiConfig, "ipc": IpcConfig, "vault": VaultConfig, "monitor": MonitorConfig, "alert": AlertConfig,
             "net": NetConfig, "probe": ProbeConfig, "wsl": WslConfig, "wechat": WechatConfig,
             "retention": RetentionConfig, "log": LogConfig}


def load(data: dict[str, Any]) -> WinAgentConfig:
    """从解析好的 TOML 字典构造配置;**未知段/未知键一律忽略**(升级只补缺省,03 §2.13「新版本新增键只补缺省」)。

    元组型键(``vpn_adapter_patterns`` 等)接受 list,内部统一存 tuple(冻结 dataclass 要求可哈希)。
    """
    kw: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        raw = data.get(name) or {}
        if not isinstance(raw, dict):
            continue
        fields = {f.name: f for f in cls.__dataclass_fields__.values()}     # type: ignore[attr-defined]
        vals: dict[str, Any] = {}
        for k, v in raw.items():
            if k not in fields:
                continue
            cur = getattr(cls(), k)
            if isinstance(cur, tuple) and isinstance(v, list):
                v = tuple(tuple(x) if isinstance(x, list) else x for x in v)
            vals[k] = v
        kw[name] = cls(**vals)
    return WinAgentConfig(**kw)


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def write_toml_keys(path: str, section: str, values: dict[str, Any]) -> str:
    """R6-88:把 ``[section]`` 下的若干**标量**键写回 ``winagent.toml``,其余内容逐字不动。

    只做本项目需要的最小事(行级编辑,不是通用 TOML 序列化):
    - 段已存在 ⇒ 段内同名键整行替换(保留行尾注释前的内容不保证,注释丢弃是可接受的);缺的键追加在段末;
    - 段不存在 ⇒ 文件末尾追加 ``[section]`` 与各键;
    - 文件不存在 ⇒ 新建;
    - 原文件的 BOM 与换行风格(CRLF/LF)原样保留;先写同目录临时文件再 ``os.replace``,中途失败不留半截。
    返回写入后的文本(便于测试断言)。
    """
    import os
    import re
    import tempfile

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split("\n")
    lines = [ln.rstrip("\r") for ln in lines]
    if lines and lines[-1] == "":
        lines.pop()

    sec_re = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")
    start = end = None
    for i, ln in enumerate(lines):
        m = sec_re.match(ln)
        if m is None:
            continue
        if start is not None:
            end = i
            break
        if m.group(1).strip() == section:
            start = i
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        start, end = len(lines) - 1, len(lines)
    if end is None:
        end = len(lines)

    pending = dict(values)
    key_re = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=")
    for i in range(start + 1, end):
        m = key_re.match(lines[i])
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)} = {_toml_scalar(pending.pop(m.group(1)))}"
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():   # 追加在段内最后一个非空行之后
        insert_at -= 1
    for k, v in pending.items():
        lines.insert(insert_at, f"{k} = {_toml_scalar(v)}")
        insert_at += 1

    out = nl.join(lines) + nl
    data = (b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8")
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".winagent-toml-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out


def wsl_memory_gb(physical_gb: int, *, cfg: WslConfig, wechat_on: bool) -> int:
    """``.wslconfig memory=`` 分档(04 §2.7.1 表,**分档单一来源=04**):≤表首取表首;超表尾按物理的 75%。"""
    table = cfg.memory_by_physical_wechat_on if wechat_on else cfg.memory_by_physical
    for phys, give in table:
        if physical_gb <= phys:
            return give
    return int(physical_gb * 0.75)
