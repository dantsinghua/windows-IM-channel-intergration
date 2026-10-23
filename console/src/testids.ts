/**
 * `data-testid` 全表(01 §4 —— 唯一出处,其它分册自造的 ID 作废,C-45)。
 *
 * 命名 `qt-{页面小写}-{元素}`;带 `{id}` 的元素在运行时拼接。
 * 页面与组件一律从本文件取 id,不在模板里手写字符串;
 * `tests/unit/testids-coverage.spec.ts` 从 docs/01 §4 反解全表并断言本文件逐条覆盖。
 */

/* ─────────────── 全局外框 shell ─────────────── */

/** 导航段名(= 路由 = 页面 ID 小写) */
export const NAV_SEGMENTS = [
  'dash', 'res', 'acct', 'screen', 'cmd', 'flow', 'msg', 'mail', 'env', 'set', 'log',
] as const
export type NavSegment = (typeof NAV_SEGMENTS)[number]

export const shell = {
  nav: (seg: NavSegment | string) => `qt-shell-nav-${seg}`,
  navCollapse: 'qt-shell-nav-collapse',
  wsDot: 'qt-shell-ws-dot',
  resChip: 'qt-shell-res-chip',
  alertBell: 'qt-shell-alert-bell',
  alertItem: (n: number | string) => `qt-shell-alert-item-${n}`,
  themeToggle: 'qt-shell-theme-toggle',
  gate: 'qt-shell-gate',
  gateRetry: 'qt-shell-gate-retry',
  gateLoginHint: 'qt-shell-gate-login-hint',
  syncBanner: 'qt-shell-sync-banner',
  watermarkBanner: 'qt-shell-watermark-banner',
} as const

/* ─────────────── P-SETUP ─────────────── */

export const setup = {
  steps: 'qt-setup-steps',
  // R6-84(2026-10-10):`qt-setup-notice-text / -fixed / -ack` 随「阅读须知」步退役,01 §4 已标「不再要求渲染」
  next: 'qt-setup-next',
  prev: 'qt-setup-prev',
  cancel: 'qt-setup-cancel',
  connAgent: 'qt-setup-conn-agent',
  connWinagent: 'qt-setup-conn-winagent',
  connRetry: 'qt-setup-conn-retry',
  selfcheckRun: 'qt-setup-selfcheck-run',
  selfcheckTable: 'qt-setup-selfcheck-table',
  loginAdd: (ch: string) => `qt-setup-login-${ch}-add`,
  loginSkip: 'qt-setup-login-skip',
  autolaunch: 'qt-setup-autolaunch',
  trayOnClose: 'qt-setup-tray-on-close',
  finish: 'qt-setup-finish',
} as const

/* ─────────────── P-DASH ─────────────── */

export const dash = {
  poolWsl: 'qt-dash-pool-wsl',
  poolWindows: 'qt-dash-pool-windows',
  canadd: (ch: string) => `qt-dash-canadd-${ch}`,
  add: (ch: string) => `qt-dash-add-${ch}`,
  wechatSwitch: 'qt-dash-wechat-switch',
  wechatSlot: 'qt-dash-wechat-slot',
  acctSummary: (channel: string) => `qt-dash-acct-summary-${channel}`,
  alertList: 'qt-dash-alert-list',
  alertItem: (n: number | string) => `qt-dash-alert-item-${n}`,
  alertItemAction: (n: number | string, act: string) => `qt-dash-alert-item-${n}-action-${act}`,
  resMem: 'qt-dash-res-mem',
  resDisk: 'qt-dash-res-disk',
  cleanup: 'qt-dash-cleanup',
  mailHealth: 'qt-dash-mail-health',
  sys: (what: string) => `qt-dash-sys-${what}`,
  emptyAdd: (channel: string) => `qt-dash-empty-add-${channel}`,
} as const

/** qt-dash-sys-{agent|winagent|winagent-user|ws|kernel|docker} */
export const DASH_SYS_DOTS = ['agent', 'winagent', 'winagent-user', 'ws', 'kernel', 'docker'] as const

/* ─────────────── P-RES(E-19) ─────────────── */

export const res = {
  refresh: 'qt-res-refresh',
  col: (which: 'host' | 'ours') => `qt-res-col-${which}`,
  memHost: (k: 'total' | 'used' | 'avail') => `qt-res-mem-host-${k}`,
  memVmmem: 'qt-res-mem-vmmem',
  memProc: (p: 'agent' | 'winagent' | 'console') => `qt-res-mem-proc-${p}`,
  memAcct: (id: string, k: 'anon' | 'current') => `qt-res-mem-acct-${id}-${k}`,
  memWechat: 'qt-res-mem-wechat',
  memChatlog: 'qt-res-mem-chatlog',
  memBudget: (ch: string) => `qt-res-mem-budget-${ch}`,
  memBudgetCalibrate: (ch: string) => `qt-res-mem-budget-${ch}-calibrate`,
  cpuHost: (k: 'cores' | 'load') => `qt-res-cpu-host-${k}`,
  cpuAcct: (id: string) => `qt-res-cpu-acct-${id}`,
  cpuProc: (name: string) => `qt-res-cpu-proc-${name}`,
  diskPart: (mount: string, k: 'total' | 'free') => `qt-res-disk-part-${mount}-${k}`,
  diskWatermark: 'qt-res-disk-watermark',
  diskDegradeList: 'qt-res-disk-degrade-list',
  diskDegrade: (action: string) => `qt-res-disk-degrade-${action}`,
  diskDir: (dir: string) => `qt-res-disk-dir-${dir}`,
  diskRetention: 'qt-res-disk-retention',
  diskVhdxDiff: 'qt-res-disk-vhdx-diff',
  chart: (k: 'mem' | 'cpu' | 'disk') => `qt-res-chart-${k}`,
  chartAlertMark: (n: number | string) => `qt-res-chart-alert-mark-${n}`,
  memWatermark: 'qt-res-mem-watermark',
  lruNote: 'qt-res-lru-note',
  lruList: 'qt-res-lru-list',
  lruRow: (id: string) => `qt-res-lru-row-${id}`,
  lruRowStop: (id: string) => `qt-res-lru-row-${id}-stop`,
  actionCleanup: 'qt-res-action-cleanup',
  actionCleanupProgress: 'qt-res-action-cleanup-progress',
  actionCleanupResult: 'qt-res-action-cleanup-result',
  actionCalibrate: 'qt-res-action-calibrate',
  actionCalibrateProgress: 'qt-res-action-calibrate-progress',
  actionCalibrateResult: 'qt-res-action-calibrate-result',
  actionOpenEnv: 'qt-res-action-open-env',
} as const

/** 我方目录占用条形的 {dir} 取值 */
export const RES_DIRS = ['db', 'media', 'mail', 'accounts', 'backup', 'vhdx'] as const

/* ─────────────── P-ACCT ─────────────── */

export const acct = {
  group: (channel: string) => `qt-acct-group-${channel}`,
  add: (ch: string) => `qt-acct-add-${ch}`,
  wechatSlot: 'qt-acct-wechat-slot',
  wechatPendingCountdown: 'qt-acct-wechat-pending-countdown',
  wechatPendingCancel: 'qt-acct-wechat-pending-cancel',
  wechatHolderFault: 'qt-acct-wechat-holder-fault',
  wechatHolderForceRelease: 'qt-acct-wechat-holder-force-release',
  wechatHolderForceReleaseConfirm: 'qt-acct-wechat-holder-force-release-confirm',
  wechatSwitch: 'qt-acct-wechat-switch',
  wechatHistorySwitch: (wxid: string) => `qt-acct-wechat-history-${wxid}-switch`,
  batchSelect: (id: string) => `qt-acct-batch-select-${id}`,
  batchStart: 'qt-acct-batch-start',
  batchStop: 'qt-acct-batch-stop',
  row: (id: string) => `qt-acct-row-${id}`,
  rowState: (id: string) => `qt-acct-row-${id}-state`,
  rowStart: (id: string) => `qt-acct-row-${id}-start`,
  rowStop: (id: string) => `qt-acct-row-${id}-stop`,
  rowRestart: (id: string) => `qt-acct-row-${id}-restart`,
  rowScreen: (id: string) => `qt-acct-row-${id}-screen`,
  rowDetail: (id: string) => `qt-acct-row-${id}-detail`,
  rowMore: (id: string) => `qt-acct-row-${id}-more`,
  rowDisable: (id: string) => `qt-acct-row-${id}-disable`,
  rowEnable: (id: string) => `qt-acct-row-${id}-enable`,
  rowDelete: (id: string) => `qt-acct-row-${id}-delete`,
  rowLogout: (id: string) => `qt-acct-row-${id}-logout`,
  rowSwitch: (id: string) => `qt-acct-row-${id}-switch`,
  deleteModal: 'qt-acct-delete-modal',
  deleteConfirm: 'qt-acct-delete-confirm',
  deleteCancel: 'qt-acct-delete-cancel',
  emptyAdd: (channel: string) => `qt-acct-empty-${channel}-add`,
  staleBanner: 'qt-acct-stale-banner',
} as const

/* ─────────────── P-ACCT-NEW ─────────────── */

export const acctNew = {
  channel: (ch: string) => `qt-acct-new-channel-${ch}`,
  steps: 'qt-acct-new-steps',
  prev: 'qt-acct-new-prev',
  next: 'qt-acct-new-next',
  cancel: 'qt-acct-new-cancel',
  resCard: 'qt-acct-new-res-card',
  resReject: 'qt-acct-new-res-reject',
  resAltQq: 'qt-acct-new-res-alt-qq',
  resAltGoacct: 'qt-acct-new-res-alt-goacct',
  label: 'qt-acct-new-label',
  profileRandom: 'qt-acct-new-profile-random',
  profilePick: 'qt-acct-new-profile-pick',
  account: 'qt-acct-new-account',
  secret: 'qt-acct-new-secret',
  secretEye: 'qt-acct-new-secret-eye',
  remember: 'qt-acct-new-remember',
  create: 'qt-acct-new-create',
  progress: 'qt-acct-new-progress',
  promptCard: 'qt-acct-new-prompt-card',
  gotoScreen: 'qt-acct-new-goto-screen',
  passwordModal: 'qt-acct-new-password-modal',
  passwordSubmit: 'qt-acct-new-password-submit',
  retry: 'qt-acct-new-retry',
  deleteBack: 'qt-acct-new-delete-back',
  stalled: 'qt-acct-new-stalled',
  qrImg: 'qt-acct-new-qr-img',
  qrExpire: 'qt-acct-new-qr-expire',
  qrRefresh: 'qt-acct-new-qr-refresh',
  qrOpenWebui: 'qt-acct-new-qr-open-webui',
  wxModuleStatus: 'qt-acct-new-wx-module-status',
  wxEnableModule: 'qt-acct-new-wx-enable-module',
  wxUserAgentHint: 'qt-acct-new-wx-user-agent-hint',
  wxHolderHint: 'qt-acct-new-wx-holder-hint',
  wxSwitchConfirm: 'qt-acct-new-wx-switch-confirm',
  wxMatch: 'qt-acct-new-wx-match',
  wxInstallPick: (n: number | string) => `qt-acct-new-wx-install-pick-${n}`,
  wxReinstallEnter: 'qt-acct-new-wx-reinstall-enter',
  wxReinstallCk: (k: 'backup' | 'autoupdate' | 'confirm') => `qt-acct-new-wx-reinstall-ck-${k}`,
  wxReinstallRun: 'qt-acct-new-wx-reinstall-run',
  wxNarratorOpen: 'qt-acct-new-wx-narrator-open',
  wxNarratorTimer: 'qt-acct-new-wx-narrator-timer',
  wxNarratorRedo: 'qt-acct-new-wx-narrator-redo',
  wxRelaunch: 'qt-acct-new-wx-relaunch',
  wxCancel: 'qt-acct-new-wx-cancel',
  wxSessionHint: 'qt-acct-new-wx-session-hint',
  wxKeyProgress: 'qt-acct-new-wx-key-progress',
  wxKeyResult: 'qt-acct-new-wx-key-result',
  wxKeyImgTimer: 'qt-acct-new-wx-key-img-timer',
  wxKeyReloginTimer: 'qt-acct-new-wx-key-relogin-timer',
  wxKeyFailHint: 'qt-acct-new-wx-key-fail-hint',
  wxKeyRetry: 'qt-acct-new-wx-key-retry',
  wxKeyReinstall: 'qt-acct-new-wx-key-reinstall',
  wxMismatchHint: 'qt-acct-new-wx-mismatch-hint',
  doneSummary: 'qt-acct-new-done-summary',
  doneGocmd: 'qt-acct-new-done-gocmd',
  doneGolist: 'qt-acct-new-done-golist',
} as const

/* ─────────────── P-ACCT-DETAIL ─────────────── */

export const ACCT_DETAIL_TABS = [
  'overview', 'runtime', 'identity', 'login', 'caps', 'settings', 'recent', 'resource',
] as const
export type AcctDetailTab = (typeof ACCT_DETAIL_TABS)[number]

export const acctDetail = {
  drawer: 'qt-acct-detail-drawer',
  tab: (t: AcctDetailTab | string) => `qt-acct-detail-tab-${t}`,
  stateCard: 'qt-acct-detail-state-card',
  stateCardLoginPhase: 'qt-acct-detail-state-card-login-phase',
  stateCardAction: (act: string) => `qt-acct-detail-state-card-action-${act}`,
  readDegraded: 'qt-acct-detail-read-degraded',
  loginPassword: 'qt-acct-detail-login-password',
  loginPasswordModal: 'qt-acct-detail-login-password-modal',
  loginPasswordSubmit: 'qt-acct-detail-login-password-submit',
  labelEdit: 'qt-acct-detail-label-edit',
  labelSave: 'qt-acct-detail-label-save',
  op: (o: 'start' | 'stop' | 'restart' | 'screen') => `qt-acct-detail-${o}`,
  delete: 'qt-acct-detail-delete',
  purge: 'qt-acct-detail-purge',
  purgeModal: 'qt-acct-detail-purge-modal',
  purgeInput: 'qt-acct-detail-purge-input',
  purgeConfirm: 'qt-acct-detail-purge-confirm',
  copyAdb: 'qt-acct-detail-copy-adb',
  adbReconnect: 'qt-acct-detail-adb-reconnect',
  streamRebuild: 'qt-acct-detail-stream-rebuild',
  health: (h: string) => `qt-acct-detail-health-${h}`,
  webuiOpen: 'qt-acct-detail-webui-open',
  webuiClose: 'qt-acct-detail-webui-close',
  webuiCountdown: 'qt-acct-detail-webui-countdown',
  exportQqdata: 'qt-acct-detail-export-qqdata',
  exportProgress: 'qt-acct-detail-export-progress',
  credStatus: 'qt-acct-detail-cred-status',
  autostop: 'qt-acct-detail-autostop',
  autostopNote: 'qt-acct-detail-autostop-note',
  credUpdate: 'qt-acct-detail-cred-update',
  credClear: 'qt-acct-detail-cred-clear',
  cap: (op: string) => `qt-acct-detail-cap-${op}`,
  settings: (k: string) => `qt-acct-detail-settings-${k}`,
  settingsSave: 'qt-acct-detail-settings-save',
  recent: (trace: string) => `qt-acct-detail-recent-${trace}`,
  resField: (k: 'quota' | 'anon' | 'current' | 'max' | 'cpu') => `qt-acct-detail-res-${k}`,
} as const

/** 账号级设置控件(05 §2.5.5) */
export const ACCT_DETAIL_SETTING_KEYS = [
  'send-interval', 'send-jitter', 'send-max', 'allowlist', 'gates', 'log-body', 'retention', 'auto-recover',
] as const

/** per-account 健康项 H04–H08 */
export const ACCT_DETAIL_HEALTH_ITEMS = ['h04', 'h05', 'h06', 'h07', 'h08'] as const

/* ─────────────── P-SCREEN ─────────────── */

export const screen = {
  thumb: (id: string) => `qt-screen-thumb-${id}`,
  thumbStart: (id: string) => `qt-screen-thumb-${id}-start`,
  qqHint: (id: string) => `qt-screen-qq-hint-${id}`,
  canvas: 'qt-screen-canvas',
  // 02 #34 控制帧没有旋转:01 §4 工具条枚举里的「旋转」按钮已不渲染(待文档方出裁决删条)
  tool: (t: 'back' | 'home' | 'shot' | 'keyboard') => `qt-screen-tool-${t}`,
  perfMenu: 'qt-screen-perf-menu',
  perf: (p: 'focus30' | 'focus15' | 'thumb10' | 'retry-hw') => `qt-screen-perf-${p}`,
  statusDecoder: 'qt-screen-status-decoder',
  statusFps: 'qt-screen-status-fps',
  statusLatency: 'qt-screen-status-latency',
  statusStreamDot: 'qt-screen-status-stream-dot',
  degradeTag: 'qt-screen-degrade-tag',
  shotPreview: 'qt-screen-shot-preview',
  shotSave: 'qt-screen-shot-save',
  loginHint: 'qt-screen-login-hint',
  // R6-72 只读提示(R 令牌 4403 / 403);01 §4 P-SCREEN 已登记
  readonlyBanner: 'qt-screen-readonly-banner',
  readonlyReconnect: 'qt-screen-readonly-reconnect',
} as const

export const SCREEN_TOOLS = ['back', 'home', 'shot', 'keyboard'] as const
export const SCREEN_PERF_ITEMS = ['focus30', 'focus15', 'thumb10', 'retry-hw'] as const

/* ─────────────── P-CMD ─────────────── */

export const cmd = {
  mode: (m: 'single' | 'broadcast') => `qt-cmd-mode-${m}`,
  account: 'qt-cmd-account',
  broadcastPick: 'qt-cmd-broadcast-pick',
  cap: (op: string) => `qt-cmd-cap-${op}`,
  capTag: (op: string) => `qt-cmd-cap-tag-${op}`,
  form: 'qt-cmd-form',
  field: (name: string) => `qt-cmd-field-${name}`,
  sessionPick: 'qt-cmd-session-pick',
  idem: 'qt-cmd-idem',
  idemRegen: 'qt-cmd-idem-regen',
  confirm: 'qt-cmd-confirm',
  timeout: 'qt-cmd-timeout',
  run: 'qt-cmd-run',
  resultCode: 'qt-cmd-result-code',
  result: (k: 'cost' | 'source' | 'state' | 'trace') => `qt-cmd-result-${k}`,
  resultOcrReview: 'qt-cmd-result-ocr-review',
  resultJson: 'qt-cmd-result-json',
  resultShot: (k: 'before' | 'after') => `qt-cmd-result-shot-${k}`,
  resultRecheck: 'qt-cmd-result-recheck',
  resultRetry: 'qt-cmd-result-retry',
  resultGohuman: 'qt-cmd-result-gohuman',
  copyCurl: 'qt-cmd-copy-curl',
  history: (n: number | string) => `qt-cmd-history-${n}`,
  historyReplay: 'qt-cmd-history-replay',
  broadcastResult: (id: string) => `qt-cmd-broadcast-result-${id}`,
} as const

/* ─────────────── P-FLOW ─────────────── */

export const flow = {
  list: 'qt-flow-list',
  row: (wf: string) => `qt-flow-row-${wf}`,
  rowRun: (wf: string) => `qt-flow-row-${wf}-run`,
  rowYaml: (wf: string) => `qt-flow-row-${wf}-yaml`,
  runAccounts: 'qt-flow-run-accounts',
  runInputs: 'qt-flow-run-inputs',
  runSubmit: 'qt-flow-run-submit',
  run: (runId: string) => `qt-flow-run-${runId}`,
  runStep: (runId: string, step: string) => `qt-flow-run-${runId}-step-${step}`,
  runPausedHint: 'qt-flow-run-paused-hint',
  stepShot: (step: string) => `qt-flow-step-${step}-shot`,
  stepGohuman: (step: string) => `qt-flow-step-${step}-gohuman`,
  runCancel: 'qt-flow-run-cancel',
} as const

/* ─────────────── P-MSG ─────────────── */

export const msg = {
  sessionList: 'qt-msg-session-list',
  session: (sid: string) => `qt-msg-session-${sid}`,
  sessionAll: 'qt-msg-session-all',
  sessionSearch: 'qt-msg-session-search',
  filter: (k: 'account' | 'dir' | 'type' | 'since' | 'until' | 'q') => `qt-msg-filter-${k}`,
  filterOcrReview: 'qt-msg-filter-ocr-review',
  qHint: 'qt-msg-q-hint',
  search: 'qt-msg-search',
  list: 'qt-msg-list',
  row: (id: string) => `qt-msg-row-${id}`,
  rowSource: (id: string) => `qt-msg-row-${id}-source`,
  rowOcrReview: (id: string) => `qt-msg-row-${id}-ocr-review`,
  rowLate: (id: string) => `qt-msg-row-${id}-late`,
  rowOriginExternal: (id: string) => `qt-msg-row-${id}-origin-external`,
  newBanner: 'qt-msg-new-banner',
  detail: 'qt-msg-detail',
  mediaPreview: 'qt-msg-media-preview',
  mediaDownload: 'qt-msg-media-download',
  mediaPending: 'qt-msg-media-pending',
  rawView: 'qt-msg-raw-view',
  export: 'qt-msg-export',
  exportWithMedia: 'qt-msg-export-with-media',
  exportProgress: 'qt-msg-export-progress',
  exportCancel: 'qt-msg-export-cancel',
  exportDownload: 'qt-msg-export-download',
} as const

/* ─────────────── P-MAIL ─────────────── */

export const MAIL_BLOCKS = ['danger', 'health', 'inbox', 'outbox', 'cleanup'] as const

export const mail = {
  block: (b: (typeof MAIL_BLOCKS)[number] | string) => `qt-mail-block-${b}`,
  disabledHint: 'qt-mail-disabled-hint',
  disabledGotoSettings: 'qt-mail-goto-settings',
  refresh: 'qt-mail-refresh',
  dangerNote: 'qt-mail-danger-note',
  dangerList: 'qt-mail-danger-list',
  dangerRow: (n: number | string) => `qt-mail-danger-row-${n}`,
  dangerRowTtl: (n: number | string) => `qt-mail-danger-row-${n}-ttl`,
  dangerRowApprove: (n: number | string) => `qt-mail-danger-row-${n}-approve`,
  dangerRowReject: (n: number | string) => `qt-mail-danger-row-${n}-reject`,
  healthCard: 'qt-mail-health-card',
  healthRoute: (scope: string) => `qt-mail-health-route-${scope}`,
  healthRouteItem: (scope: string, k: string) => `qt-mail-health-route-${scope}-${k}`,
  health: (k: string) => `qt-mail-health-${k}`,
  healthProtoFallback: 'qt-mail-health-proto-fallback',
  healthJunkHint: 'qt-mail-health-junk-hint',
  inboxFilter: (k: 'route' | 'status' | 'since' | 'until' | 'q') => `qt-mail-inbox-filter-${k}`,
  inboxRow: (n: number | string) => `qt-mail-inbox-row-${n}`,
  inboxRowRoute: (n: number | string) => `qt-mail-inbox-row-${n}-route`,
  inboxRowTrace: (n: number | string) => `qt-mail-inbox-row-${n}-trace`,
  inboxRowDetail: (n: number | string) => `qt-mail-inbox-row-${n}-detail`,
  inboxRowReparse: (n: number | string) => `qt-mail-inbox-row-${n}-reparse`,
  inboxDetailDrawer: 'qt-mail-inbox-detail-drawer',
  inboxDetail: (k: 'template' | 'op' | 'args' | 'target' | 'reqid' | 'sig') => `qt-mail-inbox-detail-${k}`,
  inboxDetailTrace: 'qt-mail-inbox-detail-trace',
  inboxDetailResult: 'qt-mail-inbox-detail-result',
  outboxRow: (n: number | string) => `qt-mail-outbox-row-${n}`,
  outboxRowResend: (n: number | string) => `qt-mail-outbox-row-${n}-resend`,
  outboxRowDiscard: (n: number | string) => `qt-mail-outbox-row-${n}-discard`,
  outboxResendReceipt: 'qt-mail-outbox-resend-receipt',
  cleanupLog: 'qt-mail-cleanup-log',
  cleanupRow: (n: number | string) => `qt-mail-cleanup-row-${n}`,
  cleanupRun: 'qt-mail-cleanup-run',
  errorBanner: 'qt-mail-error-banner',
  testmodeBanner: 'qt-mail-testmode-banner',
} as const

/** 健康块状态项 */
export const MAIL_HEALTH_ITEMS = [
  'proto', 'watermark', 'last-success', 'idle', 'failures', 'quota', 'outbox', 'cleanup',
] as const

/* ─────────────── P-ENV ─────────────── */

export const ENV_VERSION_ITEMS = [
  'console', 'agent', 'winagent', 'winagent-user', 'kernel', 'wsl', 'docker', 'distro',
] as const

export const ENV_SNAPSHOT_ITEMS = [
  'netstate', 'proxy', 'vpn', 'subnet', 'hostip', 'mtu', 'clock', 'wslconfig', 'pending-restart',
] as const

export const env = {
  version: (k: string) => `qt-env-version-${k}`,
  selfcheckRun: 'qt-env-selfcheck-run',
  selfcheckTable: 'qt-env-selfcheck-table',
  selfcheckRow: (item: string) => `qt-env-selfcheck-row-${item}`,
  snapshot: (k: string) => `qt-env-snapshot-${k}`,
  dockerCidr: 'qt-env-docker-cidr',
  dockerConflict: 'qt-env-docker-conflict',
  probeRun: 'qt-env-probe-run',
  probeRunTarget: (target: string) => `qt-env-probe-run-${target}`,
  probeTable: 'qt-env-probe-table',
  probeRow: (target: string) => `qt-env-probe-row-${target}`,
  probeSample: 'qt-env-probe-sample',
  sampleTable: 'qt-env-sample-table',
  sampleRow: (account: string, n: number | string) => `qt-env-sample-row-${account}-${n}`,
  sampleRowPick: (account: string, n: number | string) => `qt-env-sample-row-${account}-${n}-pick`,
  sampleApply: 'qt-env-sample-apply',
  sampleApplyModal: 'qt-env-sample-apply-modal',
  pubep: (k: 'ip' | 'host' | 'dns' | 'changed') => `qt-env-pubep-${k}`,
  pubepHistory: 'qt-env-pubep-history',
  pubepHistoryRow: (n: number | string) => `qt-env-pubep-history-row-${n}`,
  wxblockDomains: 'qt-env-wxblock-domains',
  wxblockState: 'qt-env-wxblock-state',
  wechatDiskWarn: 'qt-env-wechat-disk-warn',
  wxblockToggle: 'qt-env-wxblock-toggle',
  kernelReapply: 'qt-env-kernel-reapply',
  kernelRollback: 'qt-env-kernel-rollback',
  distroRepair: 'qt-env-distro-repair',
  firewallFix: 'qt-env-firewall-fix',
  dockerProxy: 'qt-env-docker-proxy',
  wslRestart: 'qt-env-wsl-restart',
  wslRestartModal: 'qt-env-wsl-restart-modal',
  diagExport: 'qt-env-diag-export',
  diagWithShots: 'qt-env-diag-with-shots',
  openLogs: 'qt-env-open-logs',
} as const

/* ─────────────── P-SET ─────────────── */

export type MailScope = 'default' | 'qidian' | 'qq' | 'wechat' | string
export const MAIL_SCOPES = ['default', 'qidian', 'qq', 'wechat'] as const

/** 每块收/发/模板控件字段名(`qt-set-mail-{scope}-*`) */
export const SET_MAIL_FIELDS = [
  'proto', 'host', 'port', 'ssl', 'user', 'pass', 'poll',
  'smtp-host', 'smtp-port', 'smtp-from', 'smtp-pass', 'recipients', 'template-id',
] as const

export const set = {
  clientsTable: 'qt-set-clients-table',
  clientsRow: (appId: string) => `qt-set-clients-row-${appId}`,
  clientsNew: 'qt-set-clients-new',
  clientsRowRotate: (appId: string) => `qt-set-clients-row-${appId}-rotate`,
  clientsRowRevoke: (appId: string) => `qt-set-clients-row-${appId}-revoke`,
  clientOnceModal: 'qt-set-client-once-modal',
  clientOnceCopy: 'qt-set-client-once-copy',
  lanEnable: 'qt-set-lan-enable',
  lanBind: 'qt-set-lan-bind',
  lanAllowlist: 'qt-set-lan-allowlist',
  lanHttps: 'qt-set-lan-https',
  lanSave: 'qt-set-lan-save',
  mailEnable: 'qt-set-mail-enable',
  mailRequireSig: 'qt-set-mail-require-sig',
  mailArchive: 'qt-set-mail-archive',
  mailSave: 'qt-set-mail-save',
  mailTestmodeBadge: 'qt-set-mail-testmode-badge',
  mailCard: (scope: MailScope) => `qt-set-mail-${scope}-card`,
  mailOverride: (scope: MailScope) => `qt-set-mail-${scope}-override`,
  mailField: (scope: MailScope, field: string) => `qt-set-mail-${scope}-${field}`,
  mailProtoEffective: (scope: MailScope) => `qt-set-mail-${scope}-proto-effective`,
  mailProtoFallback: (scope: MailScope) => `qt-set-mail-${scope}-proto-fallback`,
  mailImapRetry: (scope: MailScope) => `qt-set-mail-${scope}-imap-retry`,
  mailPop3Hint: (scope: MailScope) => `qt-set-mail-${scope}-pop3-hint`,
  mailAllowOps: (scope: MailScope) => `qt-set-mail-${scope}-allow-ops`,
  mailAllowOp: (scope: MailScope, op: string) => `qt-set-mail-${scope}-allow-ops-${op}`,
  mailAllowOpDanger: (scope: MailScope, op: string) => `qt-set-mail-${scope}-allow-ops-${op}-danger`,
  mailSender: (scope: MailScope, n: number | string) => `qt-set-mail-${scope}-sender-${n}`,
  mailSenderAdd: (scope: MailScope) => `qt-set-mail-${scope}-sender-add`,
  mailSenderRemove: (scope: MailScope) => `qt-set-mail-${scope}-sender-remove`,
  mailSenderShortname: (scope: MailScope, n: number | string) => `qt-set-mail-${scope}-sender-${n}-shortname`,
  mailSenderKeygen: (scope: MailScope, n: number | string) => `qt-set-mail-${scope}-sender-${n}-keygen`,
  mailKeyRevoke: (scope: MailScope, n: number | string) => `qt-set-mail-${scope}-sender-${n}-key-revoke`,
  mailKeyOnceModal: 'qt-set-mail-key-once-modal',
  mailKeyOnceCopy: 'qt-set-mail-key-once-copy',
  mailTest: (scope: MailScope, which: 'inbound' | 'outbound') => `qt-set-mail-${scope}-test-${which}`,
  mailroutePanel: 'qt-set-mailroute-panel',
  mailrouteToggle: 'qt-set-mailroute-toggle',
  mailrouteTable: 'qt-set-mailroute-table',
  mailrouteRow: (accountId: string) => `qt-set-mailroute-row-${accountId}`,
  mailrouteRowInherit: (accountId: string) => `qt-set-mailroute-row-${accountId}-inherit`,
  mailrouteAdd: 'qt-set-mailroute-add',
  mailrouteAddPickAccount: 'qt-set-mailroute-add-pick-account',
  mailrouteRowEdit: (accountId: string) => `qt-set-mailroute-row-${accountId}-edit`,
  mailrouteRowRemove: (accountId: string) => `qt-set-mailroute-row-${accountId}-remove`,
  mailrouteSave: 'qt-set-mailroute-save',
  mailtplOpen: 'qt-set-mailtpl-open',
  mailtplList: 'qt-set-mailtpl-list',
  mailtplRow: (tplId: string) => `qt-set-mailtpl-row-${tplId}`,
  mailtplNew: 'qt-set-mailtpl-new',
  mailtplCopy: 'qt-set-mailtpl-copy',
  mailtplDelete: 'qt-set-mailtpl-delete',
  mailtplRowSetRoute: (tplId: string) => `qt-set-mailtpl-row-${tplId}-set-route`,
  mailtplCompat: (profile: string) => `qt-set-mailtpl-compat-${profile}`,
  mailtplSubject: 'qt-set-mailtpl-subject',
  mailtplPhList: 'qt-set-mailtpl-ph-list',
  mailtplBodyList: 'qt-set-mailtpl-body-list',
  mailtplBodyRow: (n: number | string) => `qt-set-mailtpl-body-row-${n}`,
  mailtplPreview: 'qt-set-mailtpl-preview',
  mailtplPreviewPick: 'qt-set-mailtpl-preview-pick',
  mailtplPreviewSubject: 'qt-set-mailtpl-preview-subject',
  mailtplPreviewBody: 'qt-set-mailtpl-preview-body',
  mailtplInboundTable: 'qt-set-mailtpl-inbound-table',
  mailtplInboundRow: (alias: string) => `qt-set-mailtpl-inbound-row-${alias}`,
  mailtplInboundAdd: 'qt-set-mailtpl-inbound-add',
  mailtplInboundRowRemove: (alias: string) => `qt-set-mailtpl-inbound-row-${alias}-remove`,
  mailtplSave: 'qt-set-mailtpl-save',
  mailtplBack: 'qt-set-mailtpl-back',
  asr: (k: 'endpoint' | 'key' | 'concurrency' | 'min-conf') => `qt-set-asr-${k}`,
  asrSave: 'qt-set-asr-save',
  asrLlmNote: 'qt-set-asr-llm-note',
  ocrEngine: 'qt-set-ocr-engine',
  ocr: (k: 'model-dir' | 'min-conf' | 'lang') => `qt-set-ocr-${k}`,
  ocrSelftest: 'qt-set-ocr-selftest',
  ocrSave: 'qt-set-ocr-save',
  retention: (k: 'files' | 'text' | 'raw' | 'audit') => `qt-set-retention-${k}`,
  retentionSave: 'qt-set-retention-save',
  retentionTextCapHint: 'qt-set-retention-text-cap-hint',
  vaultTable: 'qt-set-vault-table',
  vaultRow: (id: string) => `qt-set-vault-row-${id}`,
  vaultRowUpdate: (id: string) => `qt-set-vault-row-${id}-update`,
  vaultRowUpdateModal: 'qt-set-vault-row-update-modal',
  vaultRowUpdateSubmit: 'qt-set-vault-row-update-submit',
  vaultRowDelete: (id: string) => `qt-set-vault-row-${id}-delete`,
  vaultRowSuspect: (id: string) => `qt-set-vault-row-${id}-suspect`,
  wxEnable: 'qt-set-wx-enable',
  wxMatrix: 'qt-set-wx-matrix',
  wxLogoutMode: 'qt-set-wx-logout-mode',
  wxKeepawake: 'qt-set-wx-keepawake',
  wxKeepawakePowercfgNote: 'qt-set-wx-keepawake-powercfg-note',
  wxSave: 'qt-set-wx-save',
  wsl: (k: 'memory' | 'processors' | 'swap') => `qt-set-wsl-${k}`,
  wslSave: 'qt-set-wsl-save',
  wslPendingBanner: 'qt-set-wsl-pending-banner',
  resQuota: (k: 'qidian' | 'qq' | 'wechat' | 'base') => `qt-set-res-${k}`,
  resCalibrate: 'qt-set-res-calibrate',
  resSave: 'qt-set-res-save',
  resMem: (k: 'warn' | 'critical') => `qt-set-res-mem-${k}`,
  autostopNote: 'qt-set-autostop-note',
  pubep: (k: 'ip' | 'host' | 'dns' | 'changed') => `qt-set-pubep-${k}`,
  pubepHostEdit: 'qt-set-pubep-host-edit',
  pubepHostSave: 'qt-set-pubep-host-save',
  pubepHistory: 'qt-set-pubep-history',
  pubepHistoryRow: (n: number | string) => `qt-set-pubep-history-row-${n}`,
  pubepWebhook: (n: number | string) => `qt-set-pubep-webhook-${n}`,
  pubepWebhookAdd: 'qt-set-pubep-webhook-add',
  pubepWebhookRemove: 'qt-set-pubep-webhook-remove',
  pubepHint: 'qt-set-pubep-hint',
  compliance: (k: 'version' | 'ack') => `qt-set-compliance-${k}`,
  about: (k: 'console' | 'agent' | 'winagent' | 'schema' | 'wa-schema') => `qt-set-about-${k}`,
  aboutMigration: 'qt-set-about-migration',
  aboutMigrationProgress: 'qt-set-about-migration-progress',
  aboutMigrationFailedDiag: 'qt-set-about-migration-failed-diag',
  aboutUpgradeNote: 'qt-set-about-upgrade-note',
  console: (k: 'theme' | 'autolaunch' | 'tray' | 'notify' | 'perf') => `qt-set-console-${k}`,
  rerunSetup: 'qt-set-rerun-setup',
} as const

/* ─────────────── P-LOG ─────────────── */

export const log = {
  tab: (k: 'command' | 'api' | 'system' | 'alerts') => `qt-log-tab-${k}`,
  filter: (k: 'account' | 'op' | 'code' | 'actor' | 'since' | 'until') => `qt-log-filter-${k}`,
  search: 'qt-log-search',
  loadMore: 'qt-log-load-more',
  table: 'qt-log-table',
  row: (n: number | string) => `qt-log-row-${n}`,
  rowTrace: (n: number | string) => `qt-log-row-${n}-trace`,
  traceDrawer: 'qt-log-trace-drawer',
  export: 'qt-log-export',
} as const

export const LOG_FILTER_KEYS = ['account', 'op', 'code', 'actor', 'since', 'until'] as const
