/**
 * `settings` store:Agent 各组设置、API 客户端、邮件 routes/模板、公网端点、
 * WinAgent 侧(微信模块 / .wslconfig / 保险库,经主进程 IPC 代调,§2.5 白名单)。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { settingsApi, systemApi } from '@/api/client'
import type { ApiClientRow, MailRouteOverride, MailTemplate, PublicEndpoint, VaultEntry } from '@/api/types'

/** 短名格式(唯一出处 = 02 #67):`^[A-Za-z0-9._-]{1,32}$`,不含冒号 */
export const SHORT_NAME_RE = /^[A-Za-z0-9._-]{1,32}$/

export interface MailScopeConfig {
  override: boolean
  proto: 'imap' | 'pop3'
  host: string
  port: number | null
  ssl: boolean
  user: string
  pass: string
  poll: number | null
  'smtp-host': string
  'smtp-port': number | null
  'smtp-from': string
  'smtp-pass': string
  recipients: string
  'template-id': string
  allow_ops: string[]
  senders: { addr: string; shortname: string; keyed: boolean }[]
}

export function emptyMailScope(): MailScopeConfig {
  return {
    override: false,
    // E-1:默认 imap
    proto: 'imap',
    host: '', port: null, ssl: true, user: '', pass: '', poll: 60,
    'smtp-host': '', 'smtp-port': null, 'smtp-from': '', 'smtp-pass': '',
    recipients: '', 'template-id': 'ibquote-163-v1',
    allow_ops: [],
    senders: [],
  }
}

export const useSettingsStore = defineStore('settings', () => {
  const groups = ref<Record<string, Record<string, unknown>>>({})
  const apiClients = ref<ApiClientRow[]>([])
  const mailScopes = ref<Record<string, MailScopeConfig>>({
    default: emptyMailScope(), qidian: emptyMailScope(), qq: emptyMailScope(), wechat: emptyMailScope(),
  })
  const mailEnabled = ref(false)
  const requireSignature = ref(true)
  const mailArchive = ref(true)
  const mailRoutes = ref<MailRouteOverride[]>([])
  const mailTemplates = ref<MailTemplate[]>([])
  const publicEndpoint = ref<PublicEndpoint | null>(null)
  const webhooks = ref<{ id: string; url: string; enabled: boolean }[]>([])
  const vault = ref<VaultEntry[]>([])
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
      mailArchive.value = mail.archive !== false
      const scopes = (mail.scopes ?? {}) as Record<string, Partial<MailScopeConfig>>
      for (const k of Object.keys(mailScopes.value)) {
        mailScopes.value[k] = { ...emptyMailScope(), ...(scopes[k] ?? {}) } as MailScopeConfig
      }
      apiClients.value = (await settingsApi.apiClients()).items
      mailRoutes.value = (await settingsApi.mailRoutes()).items
      mailTemplates.value = (await settingsApi.mailTemplates()).items
      webhooks.value = (await settingsApi.webhooks()).items
      publicEndpoint.value = await systemApi.publicEndpoint()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
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
    groups, apiClients, mailScopes, mailEnabled, requireSignature, mailArchive, mailRoutes, mailTemplates,
    publicEndpoint, webhooks, vault, wechatModule, wslConfig, wslPendingRestart, loading, error,
    allShortNames, validateShortName, loadGroup, loadAll,
    loadVault, updateVault, deleteVault, loadWechatModule, saveWechatModule, loadWslConfig, saveWslConfig,
  }
})
