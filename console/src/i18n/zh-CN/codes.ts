/**
 * 结果码 / 状态码 / 告警码 / 枚举 → 中文对照。
 *
 * 单一来源(01 §2.9 约定 6、§2.10):页面与组件一律从本文件取文案,不得自写中文。
 * 机器码枚举的唯一来源在 02(告警码 §3.7、枚举总表 §3.9)与 00(§8.1/§8.3/§8.5/§8.6);
 * 本文件只配中文与引导动作,不新增码。
 */

/* ────────────────────────── 结果码(00 §8.3 / 01 §2.10) ────────────────────────── */

export type ResultCode =
  | 'OK'
  | 'DELIVERED'
  | 'SEND_CALLED_BUT_UNCONFIRMED'
  | 'SEND_FAILED'
  | 'IDEMPOTENT_REPLAY'
  | 'GATE_BLOCKED'
  | 'UNSUPPORTED'
  | 'NOT_APPLICABLE'
  | 'NOT_READY'
  | 'TARGET_NOT_FOUND'
  | 'LOGIN_REQUIRED'
  | 'CAPTCHA_REQUIRED'
  | 'TIMEOUT'
  | 'RESOURCE_EXHAUSTED'
  | 'INVALID_ARGS'
  | 'UNAUTHORIZED'
  | 'FORBIDDEN'
  | 'RATE_LIMITED'
  | 'INTERNAL'
  | 'DISK_FULL'
  | 'CONFIRM_REQUIRED'
  | 'CONFIRM_EXPIRED'

/** 芯片色令牌(01 §2.9 设计令牌) */
export type CodeTone = 'ok' | 'pending' | 'fail' | 'blocked' | 'na'

export interface ResultCodeMeta {
  /** 中文文案 */
  zh: string
  /** 芯片色令牌 --qt-code-{tone} */
  tone: CodeTone
  /** 附带动作的说明(01 §2.10 第四列;没有则空) */
  action?: string
}

export const RESULT_CODES: Record<ResultCode, ResultCodeMeta> = {
  OK: { zh: '成功', tone: 'ok' },
  DELIVERED: { zh: '已送达(读回确认)', tone: 'ok', action: '显示 confirmed_by / confirm_ms' },
  SEND_CALLED_BUT_UNCONFIRMED: {
    zh: '已发出,期限内未读回确认(待核)',
    tone: 'pending',
    action: '再查一次(同幂等键)',
  },
  SEND_FAILED: { zh: '发送失败', tone: 'fail', action: '重试(同幂等键)' },
  IDEMPOTENT_REPLAY: { zh: '重复请求,已返回上次结果', tone: 'na' },
  GATE_BLOCKED: { zh: '被安全闸拦截(对象校验/出口词表/自定义闸)', tone: 'blocked' },
  UNSUPPORTED: { zh: '该通道不支持此能力', tone: 'na' },
  NOT_APPLICABLE: { zh: '该通道无此概念(不算失败)', tone: 'na' },
  NOT_READY: { zh: '界面或服务未就绪,稍后重试', tone: 'pending', action: '重试' },
  TARGET_NOT_FOUND: { zh: '会话/联系人/消息不存在', tone: 'fail' },
  LOGIN_REQUIRED: { zh: '账号已掉线,需要重新登录', tone: 'pending', action: '去画面' },
  CAPTCHA_REQUIRED: { zh: '需要人工完成验证', tone: 'pending', action: '去画面' },
  TIMEOUT: { zh: '超时(超过 timeout_ms)', tone: 'fail', action: '重试' },
  RESOURCE_EXHAUSTED: { zh: '资源不足', tone: 'fail', action: '显示 Agent 给的替代方案原文' },
  INVALID_ARGS: { zh: '参数错误', tone: 'fail' },
  UNAUTHORIZED: { zh: '令牌无效或已过期', tone: 'fail' },
  FORBIDDEN: { zh: '当前令牌无此权限', tone: 'fail' },
  RATE_LIMITED: { zh: '请求过于频繁', tone: 'pending', action: '自动退避后重试' },
  INTERNAL: { zh: '内部错误', tone: 'fail', action: '去环境页导出诊断包' },
  DISK_FULL: {
    zh: '本地磁盘空间不足,已暂停写入并触发清理',
    tone: 'fail',
    action: '去环境页 / 立即清理',
  },
  // 🔴 R6-58 (ca):00 §8.3 本轮补登 `CONFIRM_REQUIRED` —— 高危 op 经邮件触发后**已受理、停在待控制台确认、尚未执行**。
  //   语气用 `pending` 而不是 `fail`:它不是失败,是在等人批;文案里给出下一步该去哪儿点。
  CONFIRM_REQUIRED: { zh: '已受理,等待在控制台确认后才会执行', tone: 'pending', action: '去邮件页待确认列表' },
  CONFIRM_EXPIRED: { zh: '该确认已过期,已自动作废', tone: 'na' },
}

/**
 * `INVALID_ARGS` 的 `error.reason` 专属文案(R6-48,02 §3.10)。
 * 命中时取代通用「参数错误」,并标红对应输入框、不给「重试」。
 */
export const INVALID_ARGS_REASONS: Record<string, string> = {
  text_has_control_chars: '正文含不可见控制字符(表情转义/控制符),请删除后重发',
}

/** approve 复算失败(R6-29):不走通用「内部错误」文案 */
export const ARGS_DIGEST_MISMATCH_TEXT = '指令参数与受理时不一致,已拒绝执行(疑似数据被改动)'

export function resultCodeText(code: string, reason?: string | null): string {
  if (code === 'INVALID_ARGS' && reason && INVALID_ARGS_REASONS[reason]) {
    return INVALID_ARGS_REASONS[reason]
  }
  return RESULT_CODES[code as ResultCode]?.zh ?? code
}

export function resultCodeTone(code: string): CodeTone {
  return RESULT_CODES[code as ResultCode]?.tone ?? 'na'
}

/* ────────────────────────── 账号状态(00 §8.1 / 01 §2.6.1) ────────────────────────── */

export type AccountState =
  | 'created'
  | 'provisioning'
  | 'starting'
  | 'login_required'
  | 'logging_in'
  | 'running'
  | 'degraded'
  | 'stopping'
  | 'stopped'
  | 'error'
  | 'disabled'

/** 灯形:空心 / 旋转 / 实心 / 实心+感叹 / 实心+半环 / 实心+叉 / 虚线圈 */
export type DotShape = 'hollow' | 'spin' | 'solid' | 'bang' | 'halfring' | 'cross' | 'dashed'

export interface AccountStateMeta {
  zh: string
  /** CSS 变量名(不新增色值,处理中态借 starting,静止态借 stopped) */
  token: string
  shape: DotShape
}

export const ACCOUNT_STATES: Record<AccountState, AccountStateMeta> = {
  created: { zh: '已创建,未启动', token: '--qt-state-stopped', shape: 'hollow' },
  provisioning: { zh: '正在准备运行时(容器/APK/档案)', token: '--qt-state-starting', shape: 'spin' },
  starting: { zh: '启动中', token: '--qt-state-starting', shape: 'spin' },
  login_required: { zh: '登录中·等待你操作', token: '--qt-state-login_required', shape: 'bang' },
  logging_in: { zh: '登录中', token: '--qt-state-starting', shape: 'spin' },
  running: { zh: '在线', token: '--qt-state-running', shape: 'solid' },
  degraded: { zh: '部分能力不可用', token: '--qt-state-degraded', shape: 'halfring' },
  stopping: { zh: '停止中', token: '--qt-state-starting', shape: 'spin' },
  stopped: { zh: '已停止', token: '--qt-state-stopped', shape: 'solid' },
  error: { zh: '故障', token: '--qt-state-error', shape: 'cross' },
  disabled: { zh: '已停用,不占资源、不自动恢复', token: '--qt-state-stopped', shape: 'dashed' },
}

/** 01 §2.6.1 各状态的可用操作(界面按此禁用而不是隐藏) */
export interface StateOps {
  start: boolean
  stop: boolean
  restart: boolean
  screen: boolean
  /** IM 语义写类指令 */
  write: boolean
  /** 只读指令 get_state / screenshot */
  read: boolean
  /** 画面注入类 stream_touch|key|text|scroll(R-06) */
  inject: boolean
  del: boolean
}

const OPS = (
  start: boolean, stop: boolean, restart: boolean, screen: boolean,
  write: boolean, read: boolean, inject: boolean, del: boolean,
): StateOps => ({ start, stop, restart, screen, write, read, inject, del })

export const STATE_OPS: Record<AccountState, StateOps> = {
  created: OPS(true, false, false, false, false, false, false, true),
  provisioning: OPS(false, true, false, false, false, false, false, false),
  starting: OPS(false, true, false, true, false, false, false, false),
  // R-06:login_required 下画面可注入、只读可用,IM 写类一律禁
  login_required: OPS(false, true, true, true, false, true, true, false),
  logging_in: OPS(false, true, false, true, false, false, false, false),
  running: OPS(false, true, true, true, true, true, true, false),
  degraded: OPS(false, true, true, true, true, true, true, false),
  stopping: OPS(false, false, false, false, false, false, false, false),
  stopped: OPS(true, false, false, false, false, false, false, true),
  error: OPS(true, true, true, true, false, true, false, true),
  disabled: OPS(true, false, false, false, false, false, false, true),
}

/* ────────────────────────── state_code(01 §2.10;枚举归 05) ────────────────────────── */

/** 等人 / 掉线 / 失败异常 —— D-2 三组语义不同 */
export type StateCodeGroup = 'wait' | 'offline' | 'fail'

export interface StateCodeMeta {
  group: StateCodeGroup
  /** 等人组 = 副标题;掉线组/失败组 = 卡片标题 */
  zh: string
  /** 默认引导动作的 act 名,落 qt-acct-detail-state-card-action-{act} */
  actions: string[]
}

/** 等人组下卡片标题恒定(D-2,基线 §11.15 [LOGINPHASE]) */
export const LOGIN_PHASE_TITLE = '登录中·等待你操作'

export const STATE_CODES: Record<string, StateCodeMeta> = {
  WAIT_PASSWORD: { group: 'wait', zh: '请输入密码(未保存密码)', actions: ['password'] },
  WAIT_SMS: { group: 'wait', zh: '请在画面里输入短信验证码', actions: ['goto-screen'] },
  WAIT_CAPTCHA: { group: 'wait', zh: '请在画面里完成滑块/图形验证', actions: ['goto-screen'] },
  WAIT_DEVICE_CONFIRM: { group: 'wait', zh: '请在原设备上确认新设备登录', actions: ['goto-screen'] },
  WAIT_QRCODE: { group: 'wait', zh: '请扫码', actions: ['goto-screen'] },
  WAIT_NARRATOR: { group: 'wait', zh: '讲述人仪式进行中', actions: [] },
  WAIT_UI_TREE: { group: 'wait', zh: '微信界面仍不可见,需再做一次仪式', actions: ['narrator-redo'] },
  WAIT_KEY_IMG: { group: 'wait', zh: '请在微信里随便打开一张图片(取 img_key,≈60s 窗口)', actions: [] },
  WAIT_KEY_RELOGIN: { group: 'wait', zh: '请退出微信后重新登录(取 data_key,≈30s 窗口;不重登必失败)', actions: [] },

  KICKED: { group: 'offline', zh: '账号在其它设备登录被顶下线(不自动重登)', actions: ['login'] },
  LOGGED_OUT: { group: 'offline', zh: '微信/账号已登出,需重新登录(不自动重登)', actions: ['login'] },
  TOKEN_EXPIRED: { group: 'offline', zh: '登录态已过期(QQ),请重新登录(不自动重登)', actions: ['login'] },
  LOGIN_TIMEOUT: { group: 'offline', zh: '登录超时,已回到登录阶段,请重新发起登录', actions: ['login'] },

  BOOT_TIMEOUT: { group: 'fail', zh: '容器启动超时', actions: ['restart', 'open-env'] },
  NETWORK_UNAVAILABLE: { group: 'fail', zh: 'Android 网络检查未通过，请检查后重试启动', actions: ['open-env'] },
  APK_UNAVAILABLE: { group: 'fail', zh: '企点安装包不可用', actions: ['open-env'] },
  INSTALL_FAILED: { group: 'fail', zh: '企点安装失败', actions: ['restart', 'open-env'] },
  BAD_CREDENTIAL: { group: 'fail', zh: '账号或密码错误', actions: ['cred-update'] },
  VAULT_UNAVAILABLE: { group: 'fail', zh: '保险库不可用(WinAgent 离线)', actions: ['restart'] },
  UI_UNEXPECTED: { group: 'fail', zh: '界面出现未知页面', actions: ['goto-screen', 'open-env'] },
  ONEBOT_UNREACHABLE: { group: 'fail', zh: 'QQ 网关不可达', actions: ['restart'] },
  CONTAINER_EXIT: { group: 'fail', zh: '容器意外退出', actions: ['restart', 'open-env'] },
  NARRATOR_UNAVAILABLE: { group: 'fail', zh: '讲述人无法启动', actions: ['open-logs'] },
  KEY_FAIL: { group: 'fail', zh: '微信取钥失败:消息读取与发送均不可用', actions: ['key-retry', 'reinstall'] },
  SCREEN_LOCKED: { group: 'fail', zh: 'Windows 已锁屏,微信发送不可用', actions: ['unlock'] },
  WINAGENT_OFFLINE: {
    group: 'fail',
    zh: 'WinAgent 服务离线,微信与本机操作暂不可用(其它通道照常)',
    actions: ['open-env'],
  },
  WINAGENT_USER_OFFLINE: {
    group: 'fail',
    zh: '用户会话代理离线(未登录 Windows 桌面),微信与 wslctl 类操作暂不可用',
    actions: [],
  },
}

/** 等人组码集合 —— 决定 qt-acct-detail-state-card-login-phase 是否出现 */
export const WAIT_STATE_CODES = Object.keys(STATE_CODES).filter((c) => STATE_CODES[c].group === 'wait')
export const OFFLINE_STATE_CODES = Object.keys(STATE_CODES).filter((c) => STATE_CODES[c].group === 'offline')

export function stateCardTitle(stateCode: string | null | undefined): string {
  if (!stateCode) return ''
  const meta = STATE_CODES[stateCode]
  if (!meta) return stateCode
  return meta.group === 'wait' ? LOGIN_PHASE_TITLE : meta.zh
}

export function stateCardSubtitle(stateCode: string | null | undefined): string {
  if (!stateCode) return ''
  const meta = STATE_CODES[stateCode]
  if (!meta) return ''
  return meta.group === 'wait' ? meta.zh : ''
}

/* ────────────────────────── 告警码登记(02 §3.7 机器码单一来源) ────────────────────────── */

export type Severity = 'info' | 'warn' | 'crit'
export type AlertEventKind = 'alert' | 'mail' | 'net' | 'resource'

export interface AlertCodeMeta {
  /** 默认级别(升级由 count/持续时间触发) */
  severity: Severity
  /** 承载它的事件类型 */
  event: AlertEventKind
  /**
   * 中文常驻文案。
   * 绝大多数告警码的 title/message 由 Agent 下发(01 §2.10),故此处为 undefined;
   * 只有「需要常驻/无活告警时也要显示」的码按 N-25 判据收录固定文案。
   */
  zh?: string
}

/**
 * 02 §3.7 全表。**每个码都必须在此登记**(coverage 测试按 02 §3.7 逐行校验),
 * 但只有常驻码带 `zh`。
 */
export const ALERT_CODES: Record<string, AlertCodeMeta> = {
  H01_AGENT_API_DOWN: { severity: 'crit', event: 'alert' },
  // Agent 对 H02/H03 只发码不带 message(app.py 直接 alerts.firing),按 §2.9 约定 6 在此配中文,免得铃里甩出裸枚举。
  H02_WINAGENT_API_DOWN: {
    severity: 'crit', event: 'alert',
    zh: 'WinAgent 服务不可达(连续 3 次 ping 失败):Windows 侧服务未运行或端口不通。微信模块、凭据保险库与整机内存读数在恢复前不可用。',
  },
  H03_DOCKERD_DOWN: { severity: 'crit', event: 'alert', zh: 'WSL 内 dockerd 未运行:企点/QQ 容器无法启动或巡检,请到环境页检查发行版与 Docker 状态。' },
  H04_CONTAINER_EXITED: { severity: 'crit', event: 'alert' },
  H05_BOOT_INCOMPLETE: { severity: 'crit', event: 'alert' },
  H06_ADB_OFFLINE: { severity: 'warn', event: 'alert' },
  H07_SCRCPY_STALLED: { severity: 'warn', event: 'alert' },
  H08_NAPCAT_HEARTBEAT_LOST: { severity: 'warn', event: 'alert' },
  ACCOUNT_OFFLINE: { severity: 'warn', event: 'alert' },
  H09_CHATLOG_DOWN: { severity: 'crit', event: 'alert' },
  H10_WECHAT_PROCESS_MISSING: { severity: 'crit', event: 'alert' },
  H11_SESSION_LOCKED: { severity: 'warn', event: 'alert' },
  H12_DISK_LOW: { severity: 'warn', event: 'alert' },
  DB_WRITE_FAILED: { severity: 'crit', event: 'alert' },
  WECHAT_DISK_LOW: {
    severity: 'warn',
    event: 'alert',
    zh: '微信数据盘 {partition} 剩余不足:这是你的微信数据盘,与本程序容量治理无关;满了只会影响微信收发,请自行清理。',
  },
  H13_CLOCK_DRIFT: { severity: 'warn', event: 'alert' },
  H14_REBOOT_PENDING: { severity: 'info', event: 'alert' },
  H15_LOCALHOST_FORWARD_LOST: { severity: 'warn', event: 'alert' },
  H16_WINAGENT_BIND_MISMATCH: { severity: 'crit', event: 'alert' },
  H17_KSM_DISABLED: { severity: 'warn', event: 'alert' },
  H18_WSLCONFIG_NOT_EFFECTIVE: { severity: 'warn', event: 'alert' },
  H19_CONTAINER_MEM_HIGH: { severity: 'warn', event: 'alert' },
  H20_WECHAT_AUTO_UPDATED: { severity: 'warn', event: 'alert' },
  NET_OFFLINE: { severity: 'crit', event: 'alert' },
  ALERT_STORM: { severity: 'warn', event: 'alert' },
  WSLCONFIG_PENDING_RESTART: { severity: 'info', event: 'alert' },
  WSL_SUBNET_CHANGED: { severity: 'info', event: 'alert' },
  POOL_CALIBRATION_DRIFT: { severity: 'info', event: 'resource', zh: '资源配额与实际占用偏差较大,建议到资源页重新校准' },
  NET_STATE_CHANGED: { severity: 'info', event: 'net' },

  // ── MAIL_* 十四码(R6-33:一律用 02 §3.7 全集,小写 kind 名与 imap_fallback 作废)
  MAIL_INBOUND_STALLED: { severity: 'warn', event: 'mail', zh: '收信停滞' },
  MAIL_AUTH_FAILED: { severity: 'crit', event: 'mail', zh: '邮箱认证失败,多半是密码/授权码过期' },
  MAIL_QUOTA_HIGH: { severity: 'warn', event: 'mail', zh: '邮箱容量告急' },
  MAIL_SMTP_FAILING: { severity: 'warn', event: 'mail', zh: '发信连续失败' },
  MAIL_OUTBOX_DEAD: { severity: 'warn', event: 'mail', zh: '有邮件进死信' },
  MAIL_CLEANUP_FAILED: { severity: 'warn', event: 'mail', zh: '清理连续失败' },
  MAIL_PARSE_FAILED: { severity: 'info', event: 'mail', zh: '指令邮件解析失败' },
  MAIL_SENDER_DENIED: { severity: 'warn', event: 'mail', zh: '非白名单发件人尝试指令' },
  MAIL_WATERMARK_STALLED: { severity: 'crit', event: 'mail', zh: '水位停滞(落库反复失败)' },
  MAIL_PAUSED_DISK_FULL: {
    severity: 'crit',
    event: 'mail',
    zh: '本地磁盘已满,收信入库已暂停(邮件留在服务器不删)',
  },
  MAIL_PROTOCOL_FALLBACK: {
    severity: 'warn',
    event: 'mail',
    zh: 'IMAP 不可用,已回落 POP3(Junk 不可见、无推送)',
  },
  MAIL_ROUTE_UNRESOLVED: { severity: 'warn', event: 'mail', zh: '找不到可用的邮件路由,出站邮件未发出' },
  MAIL_ENDPOINT_CHANGED: { severity: 'info', event: 'mail', zh: '公网出口已变化' },
  MAIL_MSG_OVERSIZE: { severity: 'warn', event: 'mail', zh: '单封邮件超限,只登记元数据未取正文' },

  NET_PUBLIC_ENDPOINT_CHANGED: { severity: 'info', event: 'net' },
  WEBHOOK_DEAD: { severity: 'warn', event: 'alert' },
  EVENTS_QUEUE_OVERFLOW: { severity: 'warn', event: 'alert' },
  AUTO_RESTART_EXHAUSTED: { severity: 'crit', event: 'alert' },
  CONTAINER_OOM_KILLED: { severity: 'warn', event: 'alert' },
  MEM_PRESSURE: { severity: 'warn', event: 'resource' },
  EVENTLOOP_BLOCKED: { severity: 'warn', event: 'alert' },
  WA_VERSION_MISMATCH: { severity: 'warn', event: 'alert' },
  WA_USER_VERSION_MISMATCH: { severity: 'warn', event: 'alert' },
  WINAGENT_USER_OFFLINE: { severity: 'info', event: 'alert' },
  VAULT_ENTROPY_MISSING: { severity: 'crit', event: 'alert' },
  DB_INTEGRITY: { severity: 'crit', event: 'alert' },
  QIDIAN_NOT_ROOT: {
    severity: 'warn',
    event: 'alert',
    zh: '企点账号未取得 root,消息读取已降级(改走控件树;发送不受影响)',
  },
  // R6-50:只进铃与角标,codes.ts 不配文案、不上横幅
  QIDIAN_TABLE_DECODE_STUCK: { severity: 'warn', event: 'alert' },
  QIDIAN_MSG_GAP: { severity: 'warn', event: 'alert' },
  QIDIAN_DB_UNAVAILABLE: {
    severity: 'warn',
    event: 'alert',
    zh: '企点消息库暂不可读(路径或结构变化),消息读取已降级(改走控件树;发送不受影响)',
  },
  QIDIAN_PROFILE_FALLBACK: { severity: 'info', event: 'alert' },
  WECHAT_SWITCH_FAILED: { severity: 'warn', event: 'alert' },
  H21_WECHAT_HOSTS_BLOCK_FAILED: { severity: 'warn', event: 'alert' },
  H22_PUBLIC_ENDPOINT_CHANGED: { severity: 'info', event: 'net' },
  H23_VHDX_GROWTH: { severity: 'warn', event: 'alert' },
  H26_GUEST_PROC_CRASH: { severity: 'warn', event: 'alert' },
  DOCKER_POOL_ALL_CONFLICT: {
    severity: 'warn',
    event: 'alert',
    zh: 'docker 网段 {docker_cidr} 与本机/VPN 路由冲突:若某内网地址访问不通,可能与此网段冲突,可用安装参数 /QT_DOCKER_CIDR=<段> 重装指定,或换候选段重建 docker 网络(需确认)。',
  },
}

/** 企点「读取已降级」横幅只认这两个码(R6-50:不得扩到 STUCK/GAP) */
export const QIDIAN_READ_DEGRADED_CODES = ['QIDIAN_NOT_ROOT', 'QIDIAN_DB_UNAVAILABLE'] as const

/** P-MAIL 顶部横幅承接的六码(R6-35;MAIL_PROTOCOL_FALLBACK 不上横幅) */
export const MAIL_BANNER_CODES = [
  'MAIL_AUTH_FAILED',
  'MAIL_INBOUND_STALLED',
  'MAIL_PAUSED_DISK_FULL',
  'MAIL_WATERMARK_STALLED',
  'MAIL_ROUTE_UNRESOLVED',
  'MAIL_ENDPOINT_CHANGED',
] as const

/** hint_actions 取值(02 §3.7 末 / 04 §4);未知动作只显示不落按钮 */
export const HINT_ACTIONS: Record<string, string> = {
  wsl_restart_when_convenient: '方便时重启 WSL',
  open_env: '去环境页',
  wechat_reinstall_bundled: '用随包版本重装微信',
  fix_firewall: '修复防火墙规则',
  calibrate: '去资源页校准',
  wechat_switch: '去切换微信账号',
  retry_key: '重试取钥',
  open_account: '去账号页',
  open_mail: '去邮件页',
}

export const SEVERITY_TEXT: Record<Severity, string> = {
  info: '提示',
  warn: '警告',
  crit: '严重',
}

/* ────────────────────────── 邮件收件状态(06 §2.3.5) ────────────────────────── */

export const MAIL_INBOX_STATUS: Record<string, string> = {
  RECEIVED: '已取信',
  ACCEPTED: '已进总线',
  DONE: '已执行',
  RECEIPT_SENT: '已回执',
  RECEIPT_SKIPPED: '不回执',
  UNSUPPORTED: '非本模板',
  OUT_OF_SCOPE: '范围外(只登记)',
  OVERSIZE: '超限(只登记元数据,未取正文)',
  PARSE_FAILED: '解析失败',
  SENDER_DENIED: '发件人不在白名单',
  SIG_INVALID: '签名无效',
  EXPIRED: '已过期',
  DUPLICATE: '重复投递',
  DUPLICATE_NONCE: '重复 nonce',
  OP_DENIED: '操作不允许',
  TARGET_NOT_FOUND: '目标不存在',
  INGEST_ERROR: '落库异常',
  CONFIRM_REQUIRED: '待控制台确认',
  CONFIRM_EXPIRED: '确认已过期',
  ROUTE_MISMATCH: '账号不属于本路由',
}

/** OP_DENIED 的 reason 前缀三值(R6-29);前缀缺失/未知回落「操作不允许」 */
export const OP_DENIED_REASONS: { prefix: string; zh: string; red: boolean }[] = [
  { prefix: 'NOT_ALLOWED:', zh: '该操作未在邮件白名单内', red: false },
  { prefix: 'REJECTED:', zh: '已在控制台驳回', red: false },
  { prefix: 'ARGS_TAMPERED:', zh: ARGS_DIGEST_MISMATCH_TEXT, red: true },
]

export function mailInboxStatusText(status: string, reason?: string | null): { zh: string; red: boolean } {
  if (status === 'OP_DENIED') {
    const hit = OP_DENIED_REASONS.find((r) => (reason ?? '').startsWith(r.prefix))
    if (hit) return { zh: hit.zh, red: hit.red }
    return { zh: MAIL_INBOX_STATUS.OP_DENIED, red: false }
  }
  return { zh: MAIL_INBOX_STATUS[status] ?? status, red: false }
}

/* ────────────────────────── 探测结论(00 §8.5) ────────────────────────── */

export interface ProbeResultMeta {
  tone: 'ok' | 'warn' | 'fail' | 'na'
  zh: string
  /** 默认建议文案(可被 04 返回的 hint 覆盖) */
  hint: string
}

export const PROBE_RESULTS: Record<string, ProbeResultMeta> = {
  OK: { tone: 'ok', zh: '正常', hint: '' },
  DNS_FAIL: { tone: 'fail', zh: '域名解析失败', hint: '域名无法解析:检查 DNS 或公司 VPN 是否改写了解析' },
  TCP_TIMEOUT: {
    tone: 'fail',
    zh: '连接超时',
    hint: '目标不可达:确认公司 VPN 是否需要开启/关闭;不猜原因、不改用户网络',
  },
  TCP_REFUSED: { tone: 'fail', zh: '连接被拒', hint: '目标拒绝连接:端口或服务未开' },
  TLS_FAIL: { tone: 'fail', zh: 'TLS 失败', hint: 'TLS 握手失败:证书或中间设备拦截' },
  HTTP_4XX: { tone: 'warn', zh: 'HTTP 4xx', hint: '目标可达但拒绝(鉴权/路径)' },
  HTTP_5XX: { tone: 'warn', zh: 'HTTP 5xx', hint: '目标服务端错误,稍后重试' },
  PROXY_REQUIRED: {
    tone: 'fail',
    zh: '仅可经代理',
    hint: '当前网络无直连、只能经 HTTP 代理;企点/QQ 私有协议不走代理,无法登录(硬边界)',
  },
  BLOCKED_BY_POLICY: { tone: 'fail', zh: '被策略拦截', hint: '被本机/域策略拦截:联系 IT' },
  SKIPPED: { tone: 'na', zh: '未探测', hint: '未探测(目标未配置或 Agent 不可达)' },
}

export const PROBE_TARGETS: Record<string, string> = {
  apk_url: '企点安装包地址',
  mail_pop3: '邮箱 POP3',
  mail_imap: '邮箱 IMAP',
  mail_smtp: '邮箱 SMTP',
  qidian_msf: '企点私有协议',
  qq_servers: 'QQ 服务器',
  wechat_servers: '微信服务器',
  docker_registry: 'docker 镜像仓库',
  winagent_from_wsl: '从 WSL 连 WinAgent',
  agent_from_windows: '从 Windows 连 Agent',
}

/* ────────────────────────── 微信版本匹配(00 §8.6) ────────────────────────── */

export const WECHAT_MATCH: Record<string, string> = {
  NOT_INSTALLED: '未检测到微信',
  SUPPORTED: '版本受支持',
  UNSUPPORTED_NEWER: '微信版本过新,当前取钥组件不支持',
  UNSUPPORTED_OLDER: '微信版本过旧',
  MULTIPLE_INSTALLS: '检测到多个微信安装,请选择',
}

export const WECHAT_ACTION: Record<string, string> = {
  KEEP: '保持',
  REINSTALL_BUNDLED: '用随包版本重装',
  BLOCK: '无法继续',
}

/* ────────────────────────── 消息/结果来源(00 §7.3/§7.4,A-1) ────────────────────────── */

export const SOURCE_TEXT: Record<string, string> = {
  history: '聊天记录',
  get_msg: '单条读取',
  chatlog: 'chatlog',
  qidian_db: '企点读库',
  frida: 'frida 钩子',
  uiautomator: '控件树',
  // A-1:恒显示「截图+OCR」,界面不出现「识别模型/转录」字样
  screenshot: '截图+OCR',
  onebot: 'OneBot',
  ui: '界面操作',
  winagent: 'WinAgent',
}

export const OCR_REVIEW_TEXT = 'OCR 低置信,需复核'

/* ────────────────────────── 通道 / 网络形态 / 其它枚举 ────────────────────────── */

export type Channel = 'qidian' | 'qq' | 'wechat'

export const CHANNELS: Channel[] = ['qidian', 'qq', 'wechat']

export const CHANNEL_TEXT: Record<Channel, string> = {
  qidian: '企点',
  qq: 'QQ',
  wechat: '微信',
}

/**
 * 邮件路由列(`#58`/`#61` 的派生列 `route`)的中文显示。
 * 🔴 后端下发的是 **scope 名**(`default` / `qidian|qq|wechat` / `<account_id>`,
 * backend-api-4 §1 P-1),不是中文显示名 —— 原样渲染会在界面上甩英文枚举。
 * 认不出来的值(按账号覆盖时就是账号 id)原样显示,空值给「—」。
 */
export function mailRouteText(scope: string | null | undefined): string {
  if (!scope) return '—'
  if (scope === 'default') return '全局默认'
  return CHANNEL_TEXT[scope as Channel] ?? scope
}

export const CHANNEL_TOKEN: Record<Channel, string> = {
  qidian: '--qt-ch-qidian',
  qq: '--qt-ch-qq',
  wechat: '--qt-ch-wechat',
}

export const NET_STATE_TEXT: Record<string, string> = {
  DIRECT: '直连',
  SYSTEM_PROXY: '机器级代理',
  VPN_ACTIVE: 'VPN 已连接',
  VPN_ACTIVE_WITH_PROXY: 'VPN + 代理',
  OFFLINE: '离线',
}

export const KERNEL_STATE_TEXT: Record<string, string> = {
  DEFAULT: '系统默认内核',
  OURS: '我方内核',
  OURS_STALE: '我方内核(版本较旧)',
  OTHER_CUSTOM: '其它自定义内核',
}

export const WSL_STATE_TEXT: Record<string, string> = {
  NONE: '未安装 WSL',
  FEATURE_OFF: 'WSL 功能未开启',
  WSL1_ONLY: '仅 WSL1',
  WSL2_INBOX: 'WSL2(系统内置)',
  WSL2_STORE: 'WSL2(应用商店版)',
  VIRT_DISABLED: '虚拟化未开启',
  POLICY_BLOCKED: '被策略阻止',
}

export const JOB_STATE_TEXT: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  expired: '已过期',
}

export const WORKFLOW_STATUS_TEXT: Record<string, string> = {
  started: '进行中',
  running: '进行中',
  finished: '已完成',
  needs_human: '需人工',
  failed: '失败',
  paused: '已挂起',
  cancelled: '已取消',
}

export const CAPABILITY_KIND_TEXT: Record<string, string> = {
  read: '只读',
  write: '写',
  admin: '管理',
}

/** danger=true 的十项(02 §3.10 权威集合;本册只引用) */
export const DANGER_OPS = [
  'account_switch',
  'account_stop',
  'messages_purge',
  'account_purge',
  'account_delete',
  'settings_write',
  'vault_write',
  'workflow_run',
  'mail_cleanup_run',
  'system_wsl_restart',
] as const

/** 能力中文名(与 GET /capabilities 同一份 op;目录未给中文时回落 op 原文) */
export const CAPABILITY_TEXT: Record<string, string> = {
  read_messages: '读消息',
  list_sessions: '列会话',
  get_state: '取状态',
  screenshot: '截图',
  stream_touch: '画面点击',
  stream_key: '画面按键',
  stream_text: '画面输入文字',
  stream_scroll: '画面滚动',
  send_text: '发文本',
  send_file: '发文件',
  send_image: '发图片',
  voice_to_text: '语音转文字',
  account_switch: '切换微信账号',
  account_stop: '停止账号',
  messages_purge: '清空消息库',
  account_purge: '彻底删除账号数据',
  account_delete: '删除账号',
  settings_write: '修改配置',
  vault_write: '写保险库',
  workflow_run: '运行工作流',
  system_cleanup_run: '本地全量清理',
  mail_cleanup_run: '邮件清理(会删服务器邮件)',
  system_wsl_restart: '重启 WSL',
}

export function capabilityText(op: string): string {
  return CAPABILITY_TEXT[op] ?? op
}

/** 审计种类(02 §3.9 audit_log.kind) */
export const AUDIT_KIND_TEXT: Record<string, string> = {
  command: '指令审计',
  api: 'API 审计',
  system: '系统事件',
  stream_input: '画面注入',
}
