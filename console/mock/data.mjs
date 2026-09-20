/**
 * 开发用假数据(按 00 §7 / 02 §3.4 的出参形态造)。
 *
 * 🔴 口径:**mock 向真后端与规格看齐,不是反过来**。改这里之前先看 02 §3.4 的出参列;
 * 与真 Agent 的形状差异由 `console/tests/unit/mock-shape.spec.ts` 守着。
 */

export const now = () => new Date().toISOString()

export function makeAccounts() {
  return [
    {
      id: 'qd01', channel: 'qidian', host: 'wsl', label: '张三-固收', state: 'running',
      state_code: '', state_reason: '', error_since_ms: null, enabled: true, auto_recover: true, deleted_ms: null,
      runtime: { kind: 'redroid', container: 'qtrade-qd01', adb_port: 16001, stream_port: 16501 },
      // 🔴 M-10:00 §7.1 的 `identity` 是按通道自由形状,但 `brand/model/serialno` 三键无出处
      // (`brand/model` 属于 #24 的机型档案库);mock 不再凭空下发。
      identity: { qidian_uin: '415011447' },
      login: { mode: 'password', credential_ref: 'vault://account/qd01', remember: true },
      capabilities: ['read_messages', 'list_sessions', 'get_state', 'screenshot', 'send_text', 'send_image', 'send_file'],
      quota_mb: 2560, self_nick: '张三', self_uid: '415011447',
      created_at: now(), updated_at: now(), last_seen_at: now(),
    },
    {
      id: 'qd02', channel: 'qidian', host: 'wsl', label: '李四-利率', state: 'login_required',
      state_code: 'WAIT_SMS', state_reason: '等待短信验证', error_since_ms: null, enabled: true,
      auto_recover: true, deleted_ms: null,
      runtime: { kind: 'redroid', container: 'qtrade-qd02', adb_port: 16002, stream_port: 16502 },
      identity: {},
      login: { mode: 'password', credential_ref: null, remember: false },
      capabilities: ['get_state', 'screenshot'],
      quota_mb: 2560, created_at: now(), updated_at: now(), last_seen_at: now(),
    },
    {
      id: 'qd03', channel: 'qidian', host: 'wsl', label: '备用', state: 'stopped',
      state_code: '', state_reason: '', error_since_ms: null, enabled: true, auto_recover: false, deleted_ms: null,
      runtime: {}, identity: {}, login: { mode: 'password', remember: false }, capabilities: [],
      quota_mb: 2560, created_at: now(), updated_at: now(), last_seen_at: null,
    },
    {
      id: 'qq01', channel: 'qq', host: 'wsl', label: 'QQ-客服一号', state: 'running',
      state_code: '', state_reason: '', error_since_ms: null, enabled: true, auto_recover: true, deleted_ms: null,
      runtime: { kind: 'napcat', container: 'qtrade-qq01', ws_port: 16101, http_port: 16201 },
      identity: { qq_uin: '10001' }, login: { mode: 'qrcode', remember: false },
      capabilities: ['read_messages', 'list_sessions', 'get_state', 'send_text', 'send_image'],
      quota_mb: 614, self_nick: '客服一号', created_at: now(), updated_at: now(), last_seen_at: now(),
    },
    {
      id: 'wx01', channel: 'wechat', host: 'windows', label: '张三(微信)', state: 'running',
      state_code: '', state_reason: '', error_since_ms: null, enabled: true, auto_recover: true, deleted_ms: null,
      runtime: { kind: 'wechat_pc', wechat_version: '4.1.12.26', wxkey_dll: 'wx_key2.dll' },
      identity: {}, login: { mode: 'qrcode', remember: false },
      capabilities: ['read_messages', 'list_sessions', 'get_state', 'screenshot', 'send_text'],
      quota_mb: 1536, self_nick: '张三', wxid: 'wxid_xxx', merged_into: null,
      created_at: now(), updated_at: now(), last_seen_at: now(),
    },
    {
      id: 'wx02', channel: 'wechat', host: 'windows', label: '李四(微信)', state: 'stopped',
      state_code: '', state_reason: '', error_since_ms: null, enabled: true, auto_recover: false, deleted_ms: null,
      runtime: { wechat_version: '4.1.12.26', wxkey_dll: 'wx_key2.dll' },
      identity: {}, login: { mode: 'qrcode', remember: false }, capabilities: [],
      quota_mb: 0, wxid: 'wxid_yyy', created_at: now(), updated_at: now(), last_seen_at: null,
    },
  ]
}

export function makeResources() {
  return {
    pools: {
      wsl: { total_mb: 11264, reserved_mb: 2048, used_mb: 5120, free_mb: 4096 },
      windows: {
        total_mb: 16384, reserved_mb: 4096, wechat_mb: 1536,
        wechat_slots: {
          used: 1, max: 1, holder: 'wx01',
          pending: '', pending_expires_at: null, pending_login_session_id: '',
        },
      },
    },
    realtime: { wsl_anon_mb: 5300, win_available_mb: 6100 },
    quota_mb: { qidian: 2560, qq: 614, wechat: 1536 },
    can_add: { qidian: 1, qq: 6, wechat: 0 },
    accounts: [
      { id: 'qd01', anon_mb: 2150, current_mb: 2400, cpu_pct: 8 },
      { id: 'qq01', anon_mb: 360, current_mb: 420, cpu_pct: 3 },
    ],
  }
}

export function makeMetrics() {
  return {
    hardware: {
      mem: { total_mb: 16384, used_mb: 6758, avail_mb: 6246, vmmem_mb: 5427 },
      cpu: { logical_cores: 16, load_pct: 23 },
      disks: [
        { mount: 'C:', total_mb: 262144, free_mb: 43008 },
        { mount: 'D:', total_mb: 524288, free_mb: 3174 },
      ],
    },
    ours: {
      procs: { agent_mb: 920, winagent_mb: 310, console_mb: 410 },
      // R6-58 (aa):每进程明细(采样缺失时值给 null,不编造)
      procs_detail: [
        { name: 'qtrade-agent', rss_mb: 920, cpu_pct: 4 },
        { name: 'qtrade-winagent', rss_mb: 310, cpu_pct: 1 },
        { name: 'qtrade-console', rss_mb: 410, cpu_pct: 2 },
        { name: 'dockerd', rss_mb: 180, cpu_pct: null },
      ],
      accounts: [
        { id: 'qd01', anon_mb: 2150, current_mb: 2400, cpu_pct: 8, quota_mb: 2560 },
        { id: 'qq01', anon_mb: 360, current_mb: 420, cpu_pct: 3, quota_mb: 614 },
      ],
      wechat: { chatlog_mb: 205, wechat_pc_mb: 1430 },
      storage: { db_mb: 1228, media_mb: 8601, mail_mb: 307, accounts_mb: 12902, backup_mb: 2150, vhdx_mb: 31744 },
    },
    budget_vs_actual: [
      { id: 'qd01', quota_mb: 2560, rss_mb: 2400, drift_pct: -6 },
      { id: 'qq01', quota_mb: 614, rss_mb: 358, drift_pct: -41 },
    ],
    disk_watermark: {
      level: 'warn', free_mb: 3174, actions: [], retention_shrunk_to: null,
      last_cleanup_at: now(), last_cleanup_freed_mb: 420, vhdx_grown_mb: 13312,
    },
    mem_watermark: {
      level: 'normal', avail_mb: 6246, warn_mb: 6144, critical_mb: 3072,
      lru_suggest: [
        { id: 'qd03', last_seen_at: null, rss_mb: 2355, auto_stop_on_pressure: false },
      ],
    },
  }
}

export const CAPABILITIES = [
  { op: 'read_messages', kind: 'read', danger: false, confirmable: false,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: { session: { type: 'string', title: '会话' }, limit: { type: 'number', title: '条数', default: 20 } }, required: ['session'] } },
  { op: 'list_sessions', kind: 'read', danger: false, confirmable: false,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: {} } },
  { op: 'get_state', kind: 'read', danger: false, confirmable: false,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: {} } },
  { op: 'screenshot', kind: 'read', danger: false, confirmable: false,
    channels: { qidian: 'supported', qq: 'not_applicable', wechat: 'supported' },
    args_schema: { type: 'object', properties: {} } },
  { op: 'send_text', kind: 'write', danger: false, confirmable: true,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: { session: { type: 'string', title: '会话' }, text: { type: 'string', title: '正文' } }, required: ['session', 'text'] } },
  { op: 'send_image', kind: 'write', danger: false, confirmable: true,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: { session: { type: 'string' }, image_ref: { type: 'string' } }, required: ['session', 'image_ref'] } },
  { op: 'voice_to_text', kind: 'write', danger: false, confirmable: false,
    channels: { qidian: 'unsupported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: { message_id: { type: 'string' } }, required: ['message_id'] } },
  { op: 'account_switch', kind: 'admin', danger: true, confirmable: false,
    channels: { qidian: 'not_applicable', qq: 'not_applicable', wechat: 'supported' },
    args_schema: { type: 'object', properties: { target: { type: 'string' } } } },
  { op: 'account_stop', kind: 'admin', danger: true, confirmable: false,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: {} } },
  { op: 'workflow_run', kind: 'admin', danger: true, confirmable: false,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: { workflow: { type: 'string' } } } },
  { op: 'mail_cleanup_run', kind: 'admin', danger: true, confirmable: false,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: {} } },
  { op: 'system_cleanup_run', kind: 'admin', danger: false, confirmable: false,
    channels: { qidian: 'supported', qq: 'supported', wechat: 'supported' },
    args_schema: { type: 'object', properties: {} } },
]

export function makeMessages(n = 40) {
  // 🔴 裁决②:最后消息时间字段名 = `last_msg_at`(ISO),**不是** `last_ts`
  const sessions = [
    {
      id: 'wx01:12345@chatroom', name: '某某群', kind: 'group', account_id: 'wx01', channel: 'wechat',
      native_id: '12345@chatroom', last_msg_at: now(), msg_count: 128, unread: 2, muted: false,
    },
    {
      id: 'qq01:g_123456', name: 'QQ 报价群', kind: 'group', account_id: 'qq01', channel: 'qq',
      native_id: 'g_123456', last_msg_at: now(), msg_count: 64, unread: 0, muted: false,
    },
    {
      id: 'qd01:415011447', name: '张三-固收', kind: 'private', account_id: 'qd01', channel: 'qidian',
      native_id: '415011447', last_msg_at: now(), msg_count: 27, unread: 0, muted: false,
    },
  ]
  const out = []
  for (let i = 0; i < n; i++) {
    const s = sessions[i % sessions.length]
    const ts = new Date(Date.now() - i * 60000).toISOString()
    out.push({
      id: `msg_mock${String(i).padStart(4, '0')}`,
      ext_msg_id: `ext-${i}`,
      account_id: s.account_id, channel: s.channel,
      session: { id: s.id, name: s.name, kind: s.kind },
      dir: i % 3 === 0 ? 'out' : 'in',
      type: i % 7 === 0 ? 'image' : 'text',
      state: 'DELIVERED',
      text: i % 7 === 0 ? null : `第 ${i} 条报价:3M 1.6${i % 10}`,
      text_len: 12,
      fingerprint: `fp${i}`,
      media: i % 7 === 0 ? [{ ref: `media/2026/${i}`, sha256: `sha${i}`, mime: 'image/png', size: 2048, kind: 'image', state: 'ready' }] : [],
      sender: { id: `u${i % 5}`, name: ['张三', '李四', '王五', '我', '赵六'][i % 5] },
      self: i % 3 === 0,
      ts, received_at: ts,
      source: i % 11 === 0 ? 'screenshot' : s.channel === 'wechat' ? 'chatlog' : s.channel === 'qq' ? 'onebot' : 'qidian_db',
      revoked: i % 17 === 0,
      raw_ref: i % 5 === 0 ? `raw/${i}` : null,
      // 🔴 02 #48 的出参列**不含** `needs_review`(S-13);出向行带 confirmed_by/trace_id
      ...(i % 3 === 0 ? { confirmed_by: 'ingest_merge', trace_id: `01TRACEMSG${i}` } : {}),
    })
  }
  return { sessions, messages: out }
}
