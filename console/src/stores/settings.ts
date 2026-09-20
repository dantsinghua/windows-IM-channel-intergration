/**
 * `settings` store:Agent 各组设置、API 客户端、邮件 routes/模板、公网端点、
 * WinAgent 侧(微信模块 / .wslconfig / 保险库,经主进程 IPC 代调,§2.5 白名单)。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { mailApi, settingsApi, systemApi } from '@/api/client'
import type { ApiClientRow, MailRouteOverride, MailTemplate, PublicEndpoint, VaultEntry } from '@/api/types'

/** 短名格式(唯一出处 = 02 #67):`^[A-Za-z0-9._-]{1,32}$`,不含冒号 */
export const SHORT_NAME_RE = /^[A-Za-z0-9._-]{1,32}$/

/**
 * 🔴 `GET/PUT /settings/mail` 的**线上形状**,R6-58 (ac) 逐字定死:
 * `{enabled, require_signature, template_version, scopes:{default|qidian|qq|wechat:
 *   {override, route_id, enabled, inbound, outbound}}}`
 * 一块卡片 = 一条 `mail_routes` 行;`inbound`/`outbound` 的键名 = 02 §7.1
 * `[mail.inbound]` / `[mail.outbound]`(见 02 §3.1 `mail_routes.inbound_json/outbound_json` 注释)。
 * `secret`/`password` 类**只写不读**(读回来只有 `*_ref`)。
 */
export interface MailInboundWire {
  protocol?: 'imap' | 'pop3'
  host?: string
  port?: number | null
  ssl?: boolean
  user?: string
  /** 只读引用;写入用 `secret` */
  secret_ref?: string | null
  secret?: string
  folders?: string[]
  processed_folder?: string
  poll_interval_s?: number | null
  idle?: boolean
  keep_raw?: boolean
  allowed_senders?: string[]
  require_signature?: boolean
  allow_ops?: string[]
  scope_subject_prefix?: string[]
  [k: string]: unknown
}

export interface MailOutboundWire {
  enabled?: boolean
  host?: string
  port?: number | null
  ssl?: boolean
  user?: string
  secret_ref?: string | null
  secret?: string
  from?: string
  recipients?: string[]
  cc?: string[]
  send_rate_per_min?: number | null
  compat_title?: string
  receipt_to_sender?: boolean
  /** 出站模板(#105 `outbound_template_id`) */
  template_id?: string | null
  [k: string]: unknown
}

export interface MailScopeWire {
  override: boolean
  route_id: string | null
  enabled: boolean
  inbound: MailInboundWire
  outbound: MailOutboundWire
}

/**
 * P-SET 邮件卡片的**表单视图**(01 §4 的 `qt-set-mail-{scope}-{field}` 就是这些键)。
 * 它**不是**线上形状 —— 与 `MailScopeWire` 的互转在 `scopeFromWire` / `scopeToWire`,
 * 线上一律按 #88 的 `{override, route_id, enabled, inbound, outbound}` 走。
 */
export interface MailScopeConfig {
  override: boolean
  route_id: string | null
  enabled: boolean
  proto: 'imap' | 'pop3'
  host: string
  port: number | null
  ssl: boolean
  user: string
  /** 只写:留空即不改(读回来永远是空串) */
  pass: string
  /** 已配置的凭据引用(只读,用来显示「已配置 / 未配置」) */
  passRef: string | null
  poll: number | null
  'smtp-host': string
  'smtp-port': number | null
  'smtp-from': string
  'smtp-pass': string
  smtpPassRef: string | null
  recipients: string
  'template-id': string
  allow_ops: string[]
  /**
   * 发件人白名单。地址来自 `inbound.allowed_senders`;
   * 🔴 **短名不随本组下发**(R6-58 (ac)) —— `shortname`/`keyed` 由 `GET /mail/hmac-keys` 侧补齐。
   */
  senders: { addr: string; shortname: string; keyed: boolean }[]
}

export function emptyMailScope(): MailScopeConfig {
  return {
    override: false,
    route_id: null,
    enabled: true,
    // E-1:默认 imap
    proto: 'imap',
    host: '', port: null, ssl: true, user: '', pass: '', passRef: null, poll: 60,
    'smtp-host': '', 'smtp-port': null, 'smtp-from': '', 'smtp-pass': '', smtpPassRef: null,
    recipients: '', 'template-id': 'ibquote-163-v1',
    allow_ops: [],
    senders: [],
  }
}

/** 线上形状 → 表单视图 */
export function scopeFromWire(w: Partial<MailScopeWire> | undefined): MailScopeConfig {
  const base = emptyMailScope()
  if (!w) return base
  const i = w.inbound ?? {}
  const o = w.outbound ?? {}
  return {
    ...base,
    override: w.override === true,
    route_id: w.route_id ?? null,
    enabled: w.enabled !== false,
    proto: i.protocol ?? base.proto,
    host: i.host ?? '',
    port: i.port ?? null,
    ssl: i.ssl !== false,
    user: i.user ?? '',
    pass: '',
    passRef: i.secret_ref ?? null,
    poll: i.poll_interval_s ?? base.poll,
    'smtp-host': o.host ?? '',
    'smtp-port': o.port ?? null,
    'smtp-from': o.from ?? '',
    'smtp-pass': '',
    smtpPassRef: o.secret_ref ?? null,
    recipients: (o.recipients ?? []).join(', '),
    'template-id': o.template_id ?? base['template-id'],
    allow_ops: i.allow_ops ?? [],
    senders: (i.allowed_senders ?? []).map((addr) => ({ addr, shortname: '', keyed: false })),
  }
}

function splitList(v: string): string[] {
  return v.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean)
}

/** 表单视图 → 线上形状(密码为空即不下发那个键,保住「只写不读、留空不改」) */
export function scopeToWire(f: MailScopeConfig): MailScopeWire {
  const inbound: MailInboundWire = {
    protocol: f.proto,
    host: f.host,
    port: f.port,
    ssl: f.ssl,
    user: f.user,
    poll_interval_s: f.poll,
    allowed_senders: f.senders.map((s) => s.addr).filter(Boolean),
    allow_ops: f.allow_ops,
  }
  if (f.pass) inbound.secret = f.pass
  const outbound: MailOutboundWire = {
    host: f['smtp-host'],
    port: f['smtp-port'],
    from: f['smtp-from'],
    recipients: splitList(f.recipients),
    template_id: f['template-id'] || null,
  }
  if (f['smtp-pass']) outbound.secret = f['smtp-pass']
  return { override: f.override, route_id: f.route_id, enabled: f.enabled, inbound, outbound }
}

export const useSettingsStore = defineStore('settings', () => {
  const groups = ref<Record<string, Record<string, unknown>>>({})
  const apiClients = ref<ApiClientRow[]>([])
  const mailScopes = ref<Record<string, MailScopeConfig>>({
    default: emptyMailScope(), qidian: emptyMailScope(), qq: emptyMailScope(), wechat: emptyMailScope(),
  })
  const mailEnabled = ref(false)
  const requireSignature = ref(true)
  /**
   * 归档开关。⚠️ #88 的 `mail` 组形状里**没有** `archive` 这一键(R6-58 (ac) 逐字四键),
   * 归档保留期在 `retention` 组的 `mail_archive_days`;这里只做界面状态,不往 `mail` 组里塞。
   */
  const mailArchive = ref(true)
  const mailTemplateVersion = ref('')
  const mailRoutes = ref<MailRouteOverride[]>([])
  const mailTemplates = ref<MailTemplate[]>([])
  const publicEndpoint = ref<PublicEndpoint | null>(null)
  const webhooks = ref<{ id: string; url: string; enabled: boolean }[]>([])
  const vault = ref<VaultEntry[]>([])
  /** #67/#68 的短名表(`GET /mail/hmac-keys`) */
  const hmacKeys = ref<{ sender: string; short_name: string }[]>([])
  /** #90~#93 后端未就绪时的一句话原因(P-SET 令牌页据此显示「不可用 + 重试」而不是空表) */
  const apiClientsUnavailable = ref<string | null>(null)
  const wechatModule = ref<Record<string, unknown>>({})
  const wslConfig = ref<Record<string, unknown>>({})
  const wslPendingRestart = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** 短名**全局唯一**(跨四块与按账号覆盖面板,06 §6) */
  const allShortNames = computed(() => {
    const out: { scope: string; addr: string; shortname: string }[] = []
    for (const [scope, cfg] of Object.entries(mailScopes.value)) {
      for (const s of cfg.senders) if (s.shortname) out.push({ scope, addr: s.addr, shortname: s.shortname })
    }
    return out
  })

  /** 返回校验错误文案;通过则 null */
  function validateShortName(scope: string, index: number, value: string): string | null {
    if (!value) return '请填写短名'
    if (!SHORT_NAME_RE.test(value)) return '短名只能是字母/数字/点/下划线/连字符,长度 1~32,且不含冒号'
    const clash = allShortNames.value.find(
      (s, i) => s.shortname === value && !(s.scope === scope && indexInScope(scope, i) === index),
    )
    if (clash) return `短名已被 ${clash.scope} 的 ${clash.addr} 占用`
    return null
  }

  function indexInScope(scope: string, flatIndex: number): number {
    let n = 0
    for (const [sc, cfg] of Object.entries(mailScopes.value)) {
      for (let i = 0; i < cfg.senders.length; i++) {
        if (cfg.senders[i].shortname) {
          if (n === flatIndex) return sc === scope ? i : -1
          n++
        }
      }
    }
    return -1
  }

  async function loadGroup(name: string): Promise<Record<string, unknown>> {
    const g = await settingsApi.get(name)
    groups.value[name] = g
    return g
  }

  async function loadAll(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [api, retention, resources, asr, ocr, mail] = await Promise.all([
        loadGroup('api'), loadGroup('retention'), loadGroup('resources'),
        loadGroup('asr'), loadGroup('ocr'), loadGroup('mail'),
      ])
      void api; void retention; void resources; void asr; void ocr
      mailEnabled.value = mail.enabled === true
      requireSignature.value = mail.require_signature !== false
      mailTemplateVersion.value = String(mail.template_version ?? '')
      const scopes = (mail.scopes ?? {}) as Record<string, Partial<MailScopeWire>>
      for (const k of Object.keys(mailScopes.value)) {
        mailScopes.value[k] = scopeFromWire(scopes[k])
      }
      // 🔴 短名表只有这一处来源(R6-58 (ac):`senders[].shortname` 不随 mail 组下发)
      await loadHmacKeys()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
    // 下面这些端点后端可能还没实现(#90~#93 等):各自兜底,一个 404 不该让整页空白
    const [clients, routes, templates, hooks, pubep] = await Promise.allSettled([
      settingsApi.apiClients(), settingsApi.mailRoutes(), settingsApi.mailTemplates(),
      settingsApi.webhooks(), systemApi.publicEndpoint(),
    ])
    apiClients.value = clients.status === 'fulfilled' ? clients.value.items : []
    apiClientsUnavailable.value = clients.status === 'rejected'
      ? (clients.reason instanceof Error ? clients.reason.message : String(clients.reason))
      : null
    mailRoutes.value = routes.status === 'fulfilled' ? routes.value.items : []
    mailTemplates.value = templates.status === 'fulfilled' ? templates.value.items : []
    webhooks.value = hooks.status === 'fulfilled' ? hooks.value.items : []
    publicEndpoint.value = pubep.status === 'fulfilled' ? pubep.value : null
    loading.value = false
  }

  /** `GET /mail/hmac-keys`:短名表的唯一来源;端点未就绪时留空表(界面照常能填地址) */
  async function loadHmacKeys(): Promise<void> {
    try {
      const rows = (await mailApi.hmacKeys()).items
      hmacKeys.value = rows
      const byAddr = new Map(rows.map((r) => [r.sender, r]))
      for (const cfg of Object.values(mailScopes.value)) {
        for (const s of cfg.senders) {
          const hit = byAddr.get(s.addr)
          if (hit) {
            s.shortname = hit.short_name
            s.keyed = true
          }
        }
      }
    } catch {
      hmacKeys.value = []
    }
  }

  /* ── WinAgent 侧:一律经主进程 IPC(§2.5 白名单),渲染进程不直连 17610 ── */

  async function loadVault(): Promise<void> {
    const r = (await window.qt?.wa.invoke('vault.list', {})) as { items?: VaultEntry[] } | undefined
    vault.value = r?.items ?? []
  }

  async function updateVault(id: string, secret: string): Promise<void> {
    // 写/删 Vault 由主进程先弹原生模态确认(R-07/§11.19)
    await window.qt?.wa.invoke('vault.put', { id, secret })
    await loadVault()
  }

  async function deleteVault(id: string): Promise<void> {
    await window.qt?.wa.invoke('vault.delete', { id })
    await loadVault()
  }

  async function loadWechatModule(): Promise<void> {
    wechatModule.value = ((await window.qt?.wa.invoke('wechat.settings.get', {})) ?? {}) as Record<string, unknown>
  }

  async function saveWechatModule(patch: Record<string, unknown>): Promise<void> {
    await window.qt?.wa.invoke('wechat.settings.put', patch)
    await loadWechatModule()
  }

  async function loadWslConfig(): Promise<void> {
    wslConfig.value = ((await window.qt?.wa.invoke('wsl.config.get', {})) ?? {}) as Record<string, unknown>
  }

  async function saveWslConfig(patch: Record<string, unknown>): Promise<void> {
    const r = (await window.qt?.wa.invoke('wsl.config.put', patch)) as { pending_restart?: boolean } | undefined
    wslPendingRestart.value = !!r?.pending_restart
    await loadWslConfig()
  }

  return {
    groups, apiClients, apiClientsUnavailable, mailScopes, mailEnabled, requireSignature, mailArchive,
    mailTemplateVersion, hmacKeys, mailRoutes, mailTemplates,
    publicEndpoint, webhooks, vault, wechatModule, wslConfig, wslPendingRestart, loading, error,
    allShortNames, validateShortName, loadGroup, loadAll, loadHmacKeys,
    loadVault, updateVault, deleteVault, loadWechatModule, saveWechatModule, loadWslConfig, saveWslConfig,
  }
})
