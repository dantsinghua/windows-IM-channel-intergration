<script setup lang="ts">
/**
 * `P-SET` 设置(01 §2.7.10)。分组卡片,每组独立「保存」;
 * 密码类字段**只写不读**(输入框永远空,右侧显示「已配置 / 未配置」)。
 * 安全敏感项一律二次确认 + 审计。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { set as T, MAIL_SCOPES, SET_MAIL_FIELDS } from '@/testids'
import { useSettingsStore, emptyMailScope, scopeToWire } from '@/stores/settings'
import { useMailStore } from '@/stores/mail'
import { useEnvStore } from '@/stores/env'
import { useSessionStore } from '@/stores/session'
import { useCommandsStore } from '@/stores/commands'
import { useSetupStore } from '@/stores/setup'
import { useUiStore } from '@/stores/ui'
import { settingsApi, mailApi, systemApi, resourcesApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import {
  API_KEYS, ASR_KEYS, ASR_SECRET_KEY, OCR_KEYS, POOL_KEYS, RETENTION_KEYS, pickKeys,
} from '@/api/settingsKeys'
import { CHANNEL_TEXT, DANGER_OPS, capabilityText } from '@/i18n/zh-CN/codes'

const router = useRouter()
const store = useSettingsStore()
const mail = useMailStore()
const env = useEnvStore()
const session = useSessionStore()
const commands = useCommandsStore()
const setup = useSetupStore()
const ui = useUiStore()

/**
 * 🔴 一次性明文凭据的展示(N-1)。
 * `value` **只活在内存里**:关闭即清空(`afterClose`),不写 store、不写 localStorage、不进日志。
 * `missing` = 后端这一版没下发明文 —— 必须明说「拿不到」,不许显示一个空框假装成功。
 */
const onceModal = ref<{ open: boolean; value: string; title: string; missing: boolean }>(
  { open: false, value: '', title: '', missing: false },
)

function showOnce(title: string, value: string | null): void {
  onceModal.value = { open: true, value: value ?? '', title, missing: !value }
}

/** 关闭后从内存清除 —— 明文不留在组件状态里 */
function clearOnce(): void {
  onceModal.value = { open: false, value: '', title: '', missing: false }
}
const vaultModal = ref<{ open: boolean; id: string; secret: string }>({ open: false, id: '', secret: '' })
const routePanelOpen = ref(false)
const mailRouteDraftAccount = ref<string | undefined>()
const busy = ref(false)
/** #102 的 `configured_domain` 编辑草稿(写回落点 = `PUT /settings/api {public_domain}`) */
const pubDomainDraft = ref('')
/** ASR 密钥草稿:只写不读(输入框永远从空开始,留空即不改) */
const asrKeyDraft = ref('')

const retention = computed(() => store.groups.retention ?? {})
const apiGroup = computed(() => store.groups.api ?? {})
const asr = computed(() => store.groups.asr ?? {})
const ocr = computed(() => store.groups.ocr ?? {})
const resGroup = computed(() => store.groups.resources ?? {})
/**
 * 🔴 `resources` 组的线上形状 = **`{pools, quota_mb}`**(#88 逐字,后端 `_group_view('resources')`),
 * **不是**一堆 `*_mb` 顶层键;三通道预算在 `quota_mb.{qidian|qq|wechat}`,
 * 「WSL 基础占用」= `pools.wsl.reserved_mb`(07 §2 `[pool] wsl_reserved_mb` 的运行期真值,C-43)。
 */
const resQuota = computed(() => (resGroup.value.quota_mb ?? {}) as Record<string, number | undefined>)
const resPools = computed(() => (resGroup.value.pools ?? {}) as Record<string, Record<string, number | undefined> | undefined>)
/**
 * 🔴 内存水位阈值 `mem_warn_mb`/`mem_critical_mb` 在 **`pool` 组**(07 §2 `[pool]`,后端 `PoolConfig`),
 * 不在 `resources` 组 —— 塞进 `resources` 会被 #89 的未知键判定挡回 400。
 * (01 §4 :1345 把这两个控件写成 `PUT /settings/resources`,与 07 冲突,已转文档方订正。)
 */
const poolGroup = computed(() => store.groups.pool ?? {})
/** 合规块的数据源 = #86 `GET /system/notice`(`/settings/compliance` 不在 #88 的 group 枚举里,真后端 404) */
const notice = ref<{ notice_version: string; acked_at: string | null; ack_ms: number | null } | null>(null)
const version = computed(() => env.version)
const migration = computed(() => env.version?.migration ?? { state: 'idle' as const })

/** allow_ops 选项 = GET /capabilities 同一份目录 */
const opOptions = computed(() => commands.catalog.map((c) => ({ op: c.op, danger: c.danger })))

function scopeCfg(scope: string) {
  return store.mailScopes[scope] ?? (store.mailScopes[scope] = emptyMailScope())
}

/**
 * 保存失败的文案。🔴 `#89` 对未知键回 `400 INVALID_ARGS` 时,**哪个键不认**只在
 * `error.details[].pointer` 里(RFC 6901);不摊开就只剩一句「未知配置键」,
 * 用户与排障都看不出是哪一项 —— 这正是 P-2 那类键名笔误最需要的线索。
 */
function saveErrorText(e: unknown): string {
  if (!(e instanceof ApiFailure)) return e instanceof Error ? e.message : String(e)
  const keys = (e.detail.details ?? [])
    .map((d) => String(d.pointer ?? '').replace(/^\//, '').replace(/~1/g, '/').replace(/~0/g, '~'))
    .filter(Boolean)
  const where = keys.length ? `(涉及:${keys.join('、')})` : ''
  return `${e.message}${where}(trace ${e.traceShort})`
}

async function saveGroup(group: string, body: Record<string, unknown>): Promise<void> {
  busy.value = true
  try {
    const r = await settingsApi.put(group, body)
    // 🔴 `restart_required` 在 `data` 之外的顶层(见 client.ts 的说明)——
    // 丢了它界面就说不出「要重启才生效」,用户会以为改完立刻就算数
    //
    // 🔴 回填用的是**本次提交的值**,不是再 GET 一次(backend-api-3 §3:除 `public_domain`/`resources`
    // 外 GET 读的是「当前生效值」,重启前读回来还是旧值 ⇒ 回头 GET 会把刚填的内容洗回去,
    // 让人以为没保存上)。`restart_required:false` 的组才回头读真值。
    store.applySaved(group, body, r.restartRequired)
    if (r.restartRequired) {
      message.warning('已保存,重启 Agent 后生效(表单显示的是本次提交的值)')
    } else {
      message.success('已保存')
      await store.loadGroup(group)
    }
    for (const w of r.warnings) message.warning(w)
  } catch (e) {
    message.error(saveErrorText(e))
  } finally {
    busy.value = false
  }
}

/**
 * 保存邮箱设置。🔴 按 #88 R6-58 (ac) 的线上形状下发:
 * `{enabled, require_signature, template_version, scopes:{<s>:{override, route_id, enabled, inbound, outbound}}}`
 * ——表单的扁平键在 `scopeToWire()` 里转回 `inbound`/`outbound`(`archive` 不属本组,见 store 注释)。
 */
async function saveMail(): Promise<void> {
  const scopes: Record<string, unknown> = {}
  for (const [k, cfg] of Object.entries(store.mailScopes)) scopes[k] = scopeToWire(cfg)
  await saveGroup('mail', {
    enabled: store.mailEnabled,
    require_signature: store.requireSignature,
    template_version: store.mailTemplateVersion,
    scopes,
  })
}

/**
 * 「允许局域网访问 17600」在配置里的落点 = **`[api] bind`**(07 §2:`bind="0.0.0.0"`)——
 * 07 `[api]` 里**没有** `lan_enabled` 这个键,开关只是 `bind` 的两档视图:
 * 开 = `0.0.0.0`(监听所有网卡)、关 = `127.0.0.1`(只本机)。下拉框改的是同一个键。
 */
const lanEnabled = computed(() => String(apiGroup.value.bind ?? '127.0.0.1') !== '127.0.0.1')

function setLanEnabled(on: boolean): void {
  const g = store.groups.api ?? (store.groups.api = {})
  g.bind = on ? '0.0.0.0' : '127.0.0.1'
}

function confirmLanSave(): void {
  Modal.confirm({
    title: '开放局域网访问 17600',
    okType: 'danger',
    content: '将按端口精确放行 Windows 防火墙(不放 Any)。确认保存?',
    // 🔴 整组替换 + 未知键拒收 ⇒ 只下发 07 §2 `[api]` 登记的键
    onOk: () => saveGroup('api', pickKeys(apiGroup.value, API_KEYS)),
  })
}

async function newApiClient(): Promise<void> {
  const r = await settingsApi.createApiClient({ name: `client-${Date.now()}`, level: 'read' })
  showOnce(`新建 API 客户端 ${r.appId ?? ''}(明文只显示一次)`, r.token)
  store.apiClients = (await settingsApi.apiClients()).items
}

async function rotateApiClient(appId: string): Promise<void> {
  const r = await settingsApi.rotateApiClient(appId)
  const grace = r.graceMinutes !== null ? `,旧凭据还有 ${r.graceMinutes} 分钟宽限期` : ''
  showOnce(`${appId} 已轮换(明文只显示一次${grace})`, r.token)
}

async function revokeApiClient(appId: string): Promise<void> {
  await settingsApi.revokeApiClient(appId)
  store.apiClients = (await settingsApi.apiClients()).items
}

/** 「当前生效协议」来自 GET /mail/status 该 route(E-1),不是设置组的值 */
function routeStatus(scope: string) {
  return mail.status?.routes?.find((r) => r.scope === scope) ?? null
}

function copyText(v: string): void {
  void navigator.clipboard.writeText(v)
}

function setAutoLaunch(on: boolean): void {
  ui.autoLaunch = on
  void window.qt?.app.setAutoLaunch(on)
}

function shortNameError(scope: string, i: number): string | null {
  return store.validateShortName(scope, i, scopeCfg(scope).senders[i]?.shortname ?? '')
}

async function keygen(scope: string, i: number): Promise<void> {
  const s = scopeCfg(scope).senders[i]
  if (!s || shortNameError(scope, i)) return
  // #67:入参只有 sender + short_name,没有 route —— 短名全局唯一
  const r = await mailApi.createHmacKey(s.addr, s.shortname)
  s.keyed = true
  showOnce(`HMAC 密钥(${s.shortname},明文只显示一次)`, r.secret)
  // 重拉短名表,把 `secret_ref`/`route_id` 补上(建密钥的响应里只有一次性明文,没有这两样)
  await store.loadHmacKeys()
}

async function revokeKey(scope: string, i: number): Promise<void> {
  const s = scopeCfg(scope).senders[i]
  if (!s) return
  // #68:按短名吊销,不按 sender
  await mailApi.revokeHmacKey(s.shortname)
  s.keyed = false
  s.secret_ref = null
  s.route_id = null
  message.success('已吊销')
  // 吊销 = 删条目 ⇒ 重拉后该行就不在短名表里了(`enabled` 没有「停用但保留」这个态)
  await store.loadHmacKeys()
}

function toggleAllowOp(scope: string, op: string, danger: boolean, on: boolean): void {
  const cfg = scopeCfg(scope)
  const apply = () => {
    cfg.allow_ops = on ? [...new Set([...cfg.allow_ops, op])] : cfg.allow_ops.filter((x) => x !== op)
  }
  if (on && danger) {
    Modal.confirm({
      title: '开启危险能力的邮件触发',
      okType: 'danger',
      content: `开启后「${capabilityText(op)}」可被邮件远程触发(仍需在控制台逐条确认才执行)。`,
      onOk: apply,
    })
    return
  }
  apply()
}

async function testMail(scope: string, which: 'inbound' | 'outbound'): Promise<void> {
  const r = await mailApi.test(which, scope)
  message[r.ok ? 'success' : 'error'](r.ok ? '连通且登录成功' : '测试失败')
}

async function openVaultUpdate(id: string): Promise<void> {
  vaultModal.value = { open: true, id, secret: '' }
}

async function submitVault(): Promise<void> {
  await store.updateVault(vaultModal.value.id, vaultModal.value.secret)
  vaultModal.value = { open: false, id: '', secret: '' }
  message.success('已更新保险库条目')
}

function confirmKeepAwake(mode: string): void {
  if (mode !== 'powercfg') {
    void store.saveWechatModule({ keepawake: mode })
    return
  }
  Modal.confirm({
    title: '切换 keep-awake 到 powercfg',
    okType: 'danger',
    content: '将修改本机电源计划(台式机场景):待机与关屏超时改为「从不」,关闭微信模块或卸载时按备份还原。',
    onOk: () => store.saveWechatModule({ keepawake: 'powercfg' }),
  })
}

/**
 * 保留期。🔴 键名一律取 07 §2 `[retention]`:
 * 「库内消息正文与其余库表」= **`messages_days`**(不是 `text_days`)、
 * 「原始载荷」= **`raw_days`**(不是 `raw_enabled`)。E-18 的 30 天上限判在 `messages_days` 上。
 */
async function saveRetention(): Promise<void> {
  const days = Number(retention.value.messages_days ?? 30)
  if (days > 30) { message.error('本地只留近 30 天'); return }
  await saveGroup('retention', pickKeys(retention.value, RETENTION_KEYS))
}

function setQuota(ch: 'qidian' | 'qq' | 'wechat', v: number): void {
  const g = store.groups.resources ?? (store.groups.resources = {})
  const q = (g.quota_mb ?? (g.quota_mb = {})) as Record<string, number>
  q[ch] = v
}

function setPoolReserved(pool: 'wsl' | 'windows', v: number): void {
  const g = store.groups.resources ?? (store.groups.resources = {})
  const pools = (g.pools ?? (g.pools = {})) as Record<string, Record<string, number>>
  ;(pools[pool] ?? (pools[pool] = {})).reserved_mb = v
}

function setMemLevel(which: 'mem_warn_mb' | 'mem_critical_mb', v: number): void {
  const g = store.groups.pool ?? (store.groups.pool = {})
  g[which] = v
}

/**
 * ASR。密钥是**只写不读**的草稿(留空即不改;读回来只有 `api_key_ref`),
 * 不挂在 `groups.asr` 上 —— 挂上去会跟着整组一起被下发成一个 07 没有的野键。
 */
async function saveAsr(): Promise<void> {
  const body = pickKeys(asr.value, ASR_KEYS)
  if (asrKeyDraft.value) body[ASR_SECRET_KEY] = asrKeyDraft.value
  await saveGroup('asr', body)
  asrKeyDraft.value = ''
}

/** OCR:`engine` 只读不下发(A-1),其余三键按 07 §2 `[ocr]` */
async function saveOcr(): Promise<void> {
  await saveGroup('ocr', pickKeys(ocr.value, OCR_KEYS))
}

/** 资源池:`{pools, quota_mb}` 两键(#88);内存水位另存 `pool` 组(07 §2 `[pool]`) */
async function saveResources(): Promise<void> {
  await saveGroup('resources', { pools: resPools.value, quota_mb: resQuota.value })
}

async function saveMemLevels(): Promise<void> {
  await saveGroup('pool', pickKeys(poolGroup.value, POOL_KEYS))
}

/** 「自校准」= `POST /resources/calibrate {apply:true}`(01 §2.7.10「返回建议值,`apply=true` 写回」) */
async function calibrate(): Promise<void> {
  try {
    const r = await resourcesApi.calibrate(true)
    await store.loadGroup('resources')
    message.success(r.job_id ? `已提交自校准作业 ${r.job_id}` : '已按自校准结果写回资源池')
  } catch (e) {
    message.error(saveErrorText(e))
  }
}

/**
 * #102 的落点是 `PUT /settings/api {public_domain}`(`PUT /settings/public-endpoint` 不存在)。
 * 保存后重取一次公网端点,让 `configured_domain` 立刻反映出来。
 */
async function savePubHost(): Promise<void> {
  const r = await settingsApi.putPublicDomain(pubDomainDraft.value.trim())
  await store.loadAll()
  message.success(r.restartRequired ? '已保存配置域名,需要重启 Agent 才生效' : '已保存配置域名')
}

async function addWebhook(): Promise<void> {
  await settingsApi.addWebhook('https://')
  store.webhooks = (await settingsApi.webhooks()).items
}

async function removeWebhook(id: string): Promise<void> {
  await settingsApi.removeWebhook(id)
  store.webhooks = (await settingsApi.webhooks()).items
}

async function rerunSetup(): Promise<void> {
  await setup.rerun()
  void router.push('/setup')
}

onMounted(async () => {
  await store.loadAll()
  pubDomainDraft.value = store.publicEndpoint?.configured_domain ?? ''
  if (!commands.catalog.length) await commands.loadCatalog().catch(() => undefined)
  if (session.winagentOnline) {
    await store.loadVault().catch(() => undefined)
    await store.loadWechatModule().catch(() => undefined)
    await store.loadWslConfig().catch(() => undefined)
  }
  // 内存水位阈值在 `pool` 组(07 §2 `[pool]`),`loadAll()` 只拉了 P-SET 的六组
  await store.loadGroup('pool').catch(() => undefined)
  /*
   * 🔴 合规块的数据源 = #86 `GET /system/notice`。
   * `/settings/compliance` **不在 #88 的 group 枚举里**(真后端 404,`client.ts` 与
   * `stores/setup.ts` 早已写明),此前每次打开 P-SET 都白打一条 404、合规两行恒显示「—」(D-E)。
   * 这里直接调 API、结果只存本组件:不写 `setup` store,免得跟首启向导抢状态。
   */
  notice.value = await systemApi.notice()
    .then((n) => ({ notice_version: n.notice_version, acked_at: n.acked_at ?? null, ack_ms: n.ack_ms ?? null }))
    .catch(() => null)
  if (!env.version) await env.loadAll().catch(() => undefined)
})
</script>

<template>
  <div class="qt-page qt-stack">
    <!-- API 客户端 -->
    <section class="qt-card box">
      <div class="qt-section-title">API 客户端</div>
      <!-- #90~#93 后端未就绪时:明确说不可用 + 可重试,不给一张空表让人以为「没有客户端」 -->
      <a-alert
        v-if="store.apiClientsUnavailable"
        type="warning"
        show-icon
        class="mb"
        message="API 客户端管理暂不可用"
        :description="`Agent 侧 /settings/api-clients 尚未就绪:${store.apiClientsUnavailable}`"
      >
        <template #action><a-button size="small" @click="store.loadAll()">重试</a-button></template>
      </a-alert>
      <table class="tbl" :data-testid="T.clientsTable">
        <thead><tr><th>app_id</th><th>名称</th><th>前 6 位</th><th>权限</th><th>ip_allow</th><th>最近使用</th><th>动作</th></tr></thead>
        <tbody>
          <tr v-for="c in store.apiClients" :key="c.app_id" :data-testid="T.clientsRow(c.app_id)">
            <td class="qt-mono">{{ c.app_id }}</td>
            <td>{{ c.name }}</td>
            <td class="qt-mono">{{ c.prefix6 }}…</td>
            <td>{{ c.level }}</td>
            <td class="qt-small">{{ (c.ip_allow ?? []).join(', ') || '—' }}</td>
            <td class="qt-small">{{ c.last_used_at ?? '—' }}</td>
            <td class="qt-row">
              <a-button size="small" :data-testid="T.clientsRowRotate(c.app_id)" @click="rotateApiClient(c.app_id)">轮换</a-button>
              <a-popconfirm v-if="c.app_id !== 'console'" title="吊销该客户端?" @confirm="revokeApiClient(c.app_id)">
                <a-button size="small" danger :data-testid="T.clientsRowRevoke(c.app_id)">吊销</a-button>
              </a-popconfirm>
            </td>
          </tr>
        </tbody>
      </table>
      <a-button :data-testid="T.clientsNew" @click="newApiClient">新建</a-button>
    </section>

    <!-- 局域网开放 -->
    <section class="qt-card box">
      <div class="qt-section-title">局域网开放</div>
      <a-form layout="vertical">
        <!-- 开关与下拉改的是**同一个键** `[api] bind`(07 §2);07 里没有 `lan_enabled` -->
        <a-form-item label="允许局域网访问 17600">
          <a-switch :data-testid="T.lanEnable" :checked="lanEnabled"
                    @change="(v: any) => setLanEnabled(!!v)" />
        </a-form-item>
        <a-form-item label="绑定地址(bind)">
          <a-select :data-testid="T.lanBind" :value="apiGroup.bind ?? '127.0.0.1'"
                    :options="[{ value: '127.0.0.1', label: '127.0.0.1(仅本机)' }, { value: '0.0.0.0', label: '0.0.0.0(局域网可达)' }]"
                    @change="(v: any) => setLanEnabled(v !== '127.0.0.1')" />
        </a-form-item>
        <!--
          🔴 IP 白名单与 HTTPS:07 §2 `[api]` 里**没有**对应的配置键(IP 白名单只在 API 客户端
          逐条的 `ip_allow`;HTTPS 那三个键 `https_port`/`tls_cert`/`tls_key` 07 有、后端
          `ApiConfig` 还没有),`#89` 又对未知键回 400 ⇒ 这两项现在**保存不了**。
          与其让人填完以为存上了,不如禁用 + 明说,并把「键名待登记」转文档方/后端。
        -->
        <a-form-item label="IP 白名单(CIDR,每行一条)">
          <a-textarea :data-testid="T.lanAllowlist" :rows="2" :value="(apiGroup.ip_allow as string) ?? ''" disabled />
          <div class="qt-small qt-muted">
            07 §2 <code>[api]</code> 未登记本机 API 的 IP 白名单键,暂不可在此保存;
            按客户端限制来源请用上方「API 客户端」的 <code>ip_allow</code>。
          </div>
        </a-form-item>
        <a-form-item label="HTTPS">
          <a-switch :data-testid="T.lanHttps" :checked="!!apiGroup.https_port" disabled />
          <span class="qt-small qt-muted">
            证书状态:{{ apiGroup.tls_cert ? '已配置' : '未配置' }};
            <code>https_port/tls_cert/tls_key</code> 尚未出现在 <code>GET/PUT /settings/api</code> 的键集里,暂不可改。
          </span>
        </a-form-item>
      </a-form>
      <a-button type="primary" :data-testid="T.lanSave" @click="confirmLanSave">保存</a-button>
      <!--
        「已保存,重启后生效」标记(#89 R6-58 (ad):v1 可热加载键清单 = 空集)。
        判据 = 本次 `PUT` 回的顶层 `restart_required`,不是猜的;表单里显示的是**本次提交的值**。
        🔴 元素 id 未在 01 §4 登记 ⇒ 暂不加 `data-testid`,清单已转文档方。
      -->
      <span v-if="store.isPendingRestart('api')" class="qt-small qt-warn">已保存,重启 Agent 后生效</span>
    </section>

    <!-- 邮箱(按通道分块) -->
    <section class="qt-card box">
      <div class="qt-row">
        <div class="qt-section-title qt-grow">邮箱</div>
        <span v-if="!store.requireSignature" class="badge" :data-testid="T.mailTestmodeBadge">
          测试模式:不验签,仅允许内网白名单发件人
        </span>
        <a-button :data-testid="T.mailtplOpen" @click="router.push('/set/mail-templates')">邮件模板…</a-button>
      </div>
      <a-form layout="vertical">
        <a-form-item label="启用邮件摆渡">
          <a-switch :data-testid="T.mailEnable" v-model:checked="store.mailEnabled" />
        </a-form-item>
        <a-form-item label="要求验签(require_signature)">
          <a-switch :data-testid="T.mailRequireSig" v-model:checked="store.requireSignature" />
        </a-form-item>
        <a-form-item label="归档">
          <a-switch :data-testid="T.mailArchive" v-model:checked="store.mailArchive" />
          <span class="qt-small qt-muted">
            归档保留期在「保留期」组的 <code>mail_archive_days</code>(#88 的 mail 组不含此键)
          </span>
        </a-form-item>
      </a-form>

      <div v-for="scope in MAIL_SCOPES" :key="scope" class="qt-card scope" :data-testid="T.mailCard(scope)">
        <div class="qt-row">
          <strong class="qt-grow">{{ scope === 'default' ? '全局默认' : CHANNEL_TEXT[scope] }}</strong>
          <a-checkbox
            v-if="scope !== 'default'"
            :data-testid="T.mailOverride(scope)"
            v-model:checked="scopeCfg(scope).override"
          >本通道单独配置</a-checkbox>
        </div>

        <template v-if="scope === 'default' || scopeCfg(scope).override">
          <div class="fields">
            <label v-for="f in SET_MAIL_FIELDS" :key="f" class="field">
              <span class="qt-small qt-muted">{{ f }}</span>
              <a-input
                v-if="!['pass', 'smtp-pass', 'ssl', 'proto'].includes(f)"
                :data-testid="T.mailField(scope, f)"
                :value="(scopeCfg(scope) as any)[f]"
                @change="(e: any) => (scopeCfg(scope) as any)[f] = e.target.value"
              />
              <a-select
                v-else-if="f === 'proto'"
                :data-testid="T.mailField(scope, f)"
                :value="scopeCfg(scope).proto"
                :options="[{ value: 'imap', label: 'IMAP(推荐)' }, { value: 'pop3', label: 'POP3' }]"
                @change="(v: any) => scopeCfg(scope).proto = v"
              />
              <a-switch
                v-else-if="f === 'ssl'"
                :data-testid="T.mailField(scope, f)"
                :checked="scopeCfg(scope).ssl"
                @change="(v: any) => scopeCfg(scope).ssl = !!v"
              />
              <span v-else class="qt-row">
                <a-input
                  type="password"
                  autocomplete="new-password"
                  placeholder="只写不读:留空即不改"
                  :data-testid="T.mailField(scope, f)"
                  :value="(scopeCfg(scope) as any)[f]"
                  @change="(e: any) => (scopeCfg(scope) as any)[f] = e.target.value"
                />
                <!-- `*_ref` 是服务端唯一回读的东西(#88:secret 只写不读) -->
                <span class="qt-small qt-muted">
                  {{ (f === 'pass' ? scopeCfg(scope).passRef : scopeCfg(scope).smtpPassRef) ? '已配置' : '未配置' }}
                </span>
              </span>
            </label>
          </div>

          <div class="qt-row">
            <span :data-testid="T.mailProtoEffective(scope)" class="qt-small">
              当前生效协议:{{ routeStatus(scope)?.inbound.protocol_active ?? '—' }}
            </span>
            <span
              v-if="scopeCfg(scope).proto === 'imap' && routeStatus(scope)?.inbound.protocol_active === 'pop3'"
              class="qt-warn qt-small"
              :data-testid="T.mailProtoFallback(scope)"
            >
              已回落 POP3:{{ routeStatus(scope)?.inbound.fallback?.reason }}
              <a-button size="small" :data-testid="T.mailImapRetry(scope)" @click="testMail(scope, 'inbound')">重试 IMAP</a-button>
            </span>
          </div>
          <p v-if="scopeCfg(scope).proto === 'pop3'" class="qt-small qt-warn" :data-testid="T.mailPop3Hint(scope)">
            POP3 看不到 Junk 文件夹,被反垃圾拦下的指令邮件会漏;推荐 IMAP。
          </p>

          <!-- 发件人白名单与 HMAC 短名 -->
          <div class="qt-section-title mt">发件人白名单与 HMAC 密钥</div>
          <div v-for="(s, i) in scopeCfg(scope).senders" :key="i" class="qt-row sender" :data-testid="T.mailSender(scope, i)">
            <a-input class="w200" v-model:value="s.addr" placeholder="ops@corp" />
            <a-input
              class="w160"
              :data-testid="T.mailSenderShortname(scope, i)"
              v-model:value="s.shortname"
              :disabled="s.keyed"
              :status="shortNameError(scope, i) ? 'error' : undefined"
              placeholder="短名(全局唯一)"
            />
            <span v-if="shortNameError(scope, i)" class="qt-danger qt-small">{{ shortNameError(scope, i) }}</span>
            <a-button
              size="small"
              :disabled="!!shortNameError(scope, i) || s.keyed"
              :data-testid="T.mailSenderKeygen(scope, i)"
              @click="keygen(scope, i)"
            >生成密钥</a-button>
            <a-popconfirm title="吊销该短名的 HMAC 密钥?" @confirm="revokeKey(scope, i)">
              <a-button size="small" danger :disabled="!s.keyed" :data-testid="T.mailKeyRevoke(scope, i)">吊销</a-button>
            </a-popconfirm>
            <a-button size="small" :data-testid="T.mailSenderRemove(scope)"
                      @click="scopeCfg(scope).senders.splice(i, 1)">删除</a-button>
            <!--
              🔴 短名表的唯一来源是 `GET /mail/hmac-keys`(R6-58 (ac)),它**只回 `secret_ref` 不回密钥值** ——
              这里显示引用与所属路由,是为了让人看得出「这条已经有密钥、存在保险库的哪一格」,
              明文只在 #67 建密钥那一次弹过(关闭即清,见 `showOnce`)。全局短名的 `route_id` 是 null。
            -->
            <span v-if="s.keyed" class="qt-small qt-muted qt-mono">
              {{ s.secret_ref ?? '(无 secret_ref)' }} · 路由 {{ s.route_id ?? '全局' }}
            </span>
          </div>
          <a-button
            size="small"
            :data-testid="T.mailSenderAdd(scope)"
            @click="scopeCfg(scope).senders.push({ addr: '', shortname: '', keyed: false })"
          >新增发件人</a-button>

          <!-- allow_ops:默认白名单 = 所有 danger=false -->
          <div class="qt-section-title mt">允许的能力(allow_ops)</div>
          <div class="ops" :data-testid="T.mailAllowOps(scope)">
            <label v-for="o in opOptions" :key="o.op" class="opitem">
              <a-checkbox
                :data-testid="T.mailAllowOp(scope, o.op)"
                :checked="scopeCfg(scope).allow_ops.includes(o.op)"
                @change="(e: any) => toggleAllowOp(scope, o.op, o.danger, e.target.checked)"
              >{{ capabilityText(o.op) }}</a-checkbox>
              <span v-if="o.danger" class="danger-tag" :data-testid="T.mailAllowOpDanger(scope, o.op)">
                危险·可被邮件远程触发
              </span>
            </label>
            <p class="qt-small qt-muted">
              默认白名单 = 所有 danger=false 的能力;danger 十项({{ DANGER_OPS.join('、') }})须逐条勾选。
            </p>
          </div>

          <div class="qt-row mt">
            <a-button size="small" :data-testid="T.mailTest(scope, 'inbound')" @click="testMail(scope, 'inbound')">测试收信</a-button>
            <a-button size="small" :data-testid="T.mailTest(scope, 'outbound')" @click="testMail(scope, 'outbound')">测试发信</a-button>
          </div>
        </template>
      </div>

      <!-- 按账号覆盖(高级,默认折叠) -->
      <a-collapse v-model:activeKey="routePanelOpen" :data-testid="T.mailroutePanel">
        <a-collapse-panel key="route" header="按账号覆盖(高级)">
          <template #extra><span :data-testid="T.mailrouteToggle" /></template>
          <table class="tbl" :data-testid="T.mailrouteTable">
            <thead><tr><th>账号</th><th>通道</th><th>继承自</th><th>启用</th><th>动作</th></tr></thead>
            <tbody>
              <tr v-for="r in store.mailRoutes" :key="r.account_id" :data-testid="T.mailrouteRow(r.account_id)">
                <td>{{ r.account_id }}</td>
                <td>{{ CHANNEL_TEXT[r.channel] }}</td>
                <td :data-testid="T.mailrouteRowInherit(r.account_id)">继承自:{{ r.inherit_from }}块</td>
                <td>{{ r.enabled ? '是' : '否' }}</td>
                <td class="qt-row">
                  <a-button size="small" :data-testid="T.mailrouteRowEdit(r.account_id)">编辑</a-button>
                  <a-button size="small" danger :data-testid="T.mailrouteRowRemove(r.account_id)"
                            @click="store.mailRoutes = store.mailRoutes.filter((x) => x.account_id !== r.account_id)">删除</a-button>
                </td>
              </tr>
              <tr v-if="!store.mailRoutes.length"><td colspan="5" class="qt-muted">暂无按账号覆盖</td></tr>
            </tbody>
          </table>
          <div class="qt-row">
            <a-select
              class="w160"
              v-model:value="mailRouteDraftAccount"
              :data-testid="T.mailrouteAddPickAccount"
              placeholder="选择账号"
              :options="[]"
            />
            <a-button size="small" :data-testid="T.mailrouteAdd">新增覆盖</a-button>
            <a-popconfirm title="保存按账号覆盖?优先级 账号 > 通道 > 全局" @confirm="settingsApi.putMailRoutes(store.mailRoutes)">
              <a-button size="small" type="primary" :data-testid="T.mailrouteSave">保存</a-button>
            </a-popconfirm>
          </div>
        </a-collapse-panel>
      </a-collapse>

      <a-button type="primary" :loading="busy" :data-testid="T.mailSave" @click="saveMail">保存邮箱设置</a-button>
      <span v-if="store.isPendingRestart('mail')" class="qt-small qt-warn">已保存,重启 Agent 后生效</span>
    </section>

    <!-- ASR -->
    <section class="qt-card box">
      <div class="qt-section-title">ASR</div>
      <p class="qt-warn qt-small" :data-testid="T.asrLlmNote">
        语音转文字是本程序唯一使用大模型的地方;端点与密钥由使用方自行配置,默认指向内网;不配则 voice_to_text 回 UNSUPPORTED。
      </p>
      <a-form layout="vertical">
        <a-form-item label="端点">
          <a-input :data-testid="T.asr('endpoint')" :value="asr.endpoint as string"
                   @change="(e: any) => asr.endpoint = e.target.value" />
        </a-form-item>
        <a-form-item label="密钥(只写不读:留空即不改)">
          <span class="qt-row">
            <a-input :data-testid="T.asr('key')" type="password" autocomplete="new-password"
                     placeholder="留空即不改"
                     :value="asrKeyDraft" @change="(e: any) => asrKeyDraft = e.target.value" />
            <!-- 服务端唯一回读的是 `api_key_ref`(07 §2 `[asr]`),不是密钥值 -->
            <span class="qt-small qt-muted">{{ asr.api_key_ref ? '已配置' : '未配置' }}</span>
          </span>
        </a-form-item>
        <a-form-item label="并发">
          <a-input-number :data-testid="T.asr('concurrency')" :value="asr.concurrency as number"
                          @change="(v: any) => asr.concurrency = v" />
        </a-form-item>
        <a-form-item label="min_confidence">
          <a-input-number :data-testid="T.asr('min-conf')" :step="0.05" :value="asr.min_confidence as number"
                          @change="(v: any) => asr.min_confidence = v" />
        </a-form-item>
      </a-form>
      <a-button type="primary" :data-testid="T.asrSave" @click="saveAsr">保存</a-button>
      <span v-if="store.isPendingRestart('asr')" class="qt-small qt-warn">已保存,重启 Agent 后生效</span>
    </section>

    <!-- OCR(离线) -->
    <section class="qt-card box">
      <div class="qt-section-title">OCR</div>
      <p :data-testid="T.ocrEngine">引擎:离线传统 OCR(随 rootfs 打包,不出网)—— 引擎不可选、不可填任何端点。</p>
      <a-form layout="vertical">
        <a-form-item label="model_dir">
          <a-input :data-testid="T.ocr('model-dir')" :value="ocr.model_dir as string"
                   @change="(e: any) => ocr.model_dir = e.target.value" />
        </a-form-item>
        <a-form-item label="min_conf(低于此值的行在消息列表标「需复核」,数字/金额尤其)">
          <a-input-number :data-testid="T.ocr('min-conf')" :step="0.05" :value="(ocr.min_conf as number) ?? 0.8"
                          @change="(v: any) => ocr.min_conf = v" />
        </a-form-item>
        <a-form-item label="lang">
          <a-input :data-testid="T.ocr('lang')" :value="ocr.lang as string"
                   @change="(e: any) => ocr.lang = e.target.value" />
        </a-form-item>
      </a-form>
      <div class="qt-row">
        <a-button :data-testid="T.ocrSelftest" @click="systemApi.selftestRun()">自检</a-button>
        <a-button type="primary" :data-testid="T.ocrSave" @click="saveOcr">保存</a-button>
        <span v-if="store.isPendingRestart('ocr')" class="qt-small qt-warn">已保存,重启 Agent 后生效</span>
      </div>
    </section>

    <!-- 保留期 -->
    <section class="qt-card box">
      <div class="qt-section-title">保留期</div>
      <a-form layout="vertical">
        <a-form-item label="采集侧文件(媒体、原始载荷、邮件归档)保留天数">
          <a-input-number :data-testid="T.retention('files')" :value="(retention.files_days as number) ?? 7"
                          @change="(v: any) => retention.files_days = v" />
        </a-form-item>
        <!-- 🔴 键名以 07 §2 `[retention]` 为准:`messages_days`(不是 `text_days`) -->
        <a-form-item label="库内消息正文与其余库表保留天数(上限 30)">
          <a-input-number :data-testid="T.retention('text')" :max="30" :value="(retention.messages_days as number) ?? 30"
                          @change="(v: any) => retention.messages_days = v" />
          <div class="qt-small qt-warn" :data-testid="T.retentionTextCapHint">本地只留近 30 天</div>
        </a-form-item>
        <!--
          🔴 `raw_days`(不是 `raw_enabled`):07 §2 `[retention]` 里与原始载荷有关的键只有保留天数;
          「是否落盘」是另一个设置(`[messages] raw_payload`),不在本组,01 §2.7.10 正文与 §4 元素表
          的这处出入已转文档方。
        -->
        <a-form-item label="原始载荷保留天数">
          <a-input-number :data-testid="T.retention('raw')" :min="0" :value="(retention.raw_days as number) ?? 7"
                          @change="(v: any) => retention.raw_days = v" />
        </a-form-item>
        <a-form-item label="审计保留天数">
          <a-input-number :data-testid="T.retention('audit')" :max="30" :value="(retention.audit_days as number) ?? 30"
                          @change="(v: any) => retention.audit_days = v" />
        </a-form-item>
      </a-form>
      <p class="qt-small qt-muted">
        每账号可在账号详情覆盖(同样不超过 30);磁盘吃紧时保留期会被水位自动压缩(30→14→7),压缩状态见 P-RES。
      </p>
      <a-button type="primary" :data-testid="T.retentionSave" @click="saveRetention">保存</a-button>
      <span v-if="store.isPendingRestart('retention')" class="qt-small qt-warn">已保存,重启 Agent 后生效</span>
    </section>

    <!-- 保险库(WinAgent) -->
    <section class="qt-card box">
      <div class="qt-section-title">保险库(WinAgent)</div>
      <table class="tbl" :data-testid="T.vaultTable">
        <thead><tr><th>条目</th><th>scope</th><th>版本</th><th>更新</th><th>最近读取</th><th>动作</th></tr></thead>
        <tbody>
          <tr v-for="e in store.vault" :key="e.id" :data-testid="T.vaultRow(e.id)">
            <td class="qt-mono qt-small">
              {{ e.id }}
              <span v-if="e.suspect" class="qt-danger qt-small" :data-testid="T.vaultRowSuspect(e.id)">
                最近登录报密码错,请更新
              </span>
            </td>
            <td>{{ e.scope }}</td>
            <td>v{{ e.version }}</td>
            <td class="qt-small">{{ e.updated }}</td>
            <td class="qt-small">{{ e.last_read ?? '—' }}</td>
            <td class="qt-row">
              <a-button size="small" :data-testid="T.vaultRowUpdate(e.id)" @click="openVaultUpdate(e.id)">更新</a-button>
              <a-popconfirm title="删除该保险库条目?" @confirm="store.deleteVault(e.id)">
                <a-button size="small" danger :data-testid="T.vaultRowDelete(e.id)">删除</a-button>
              </a-popconfirm>
            </td>
          </tr>
          <tr v-if="!store.vault.length">
            <td colspan="6" class="qt-muted">{{ session.winagentOnline ? '暂无条目' : 'WinAgent 离线' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="qt-small qt-muted">没有「显示」:控制台令牌对保险库只能写与删,read 恒 403。</p>
    </section>

    <!-- 微信模块(WinAgent) -->
    <section class="qt-card box">
      <div class="qt-section-title">微信模块(WinAgent)</div>
      <a-form layout="vertical">
        <a-form-item label="启用微信模块">
          <a-switch :data-testid="T.wxEnable" :checked="!!store.wechatModule.enabled"
                    :disabled="!session.winagentOnline"
                    @change="(v: any) => store.saveWechatModule({ enabled: !!v })" />
        </a-form-item>
        <a-form-item label="随包微信版本与 DLL 对应表(只读)">
          <pre class="qt-mono mini" :data-testid="T.wxMatrix">{{ JSON.stringify(store.wechatModule.matrix ?? {}, null, 2) }}</pre>
        </a-form-item>
        <a-form-item label="logout_mode">
          <a-select :data-testid="T.wxLogoutMode" :value="store.wechatModule.logout_mode ?? 'kill'"
                    :options="[{ value: 'kill', label: '结束进程(默认)' }, { value: 'ui', label: 'UI 退出' }]"
                    @change="(v: any) => store.saveWechatModule({ logout_mode: v })" />
        </a-form-item>
        <a-form-item label="keep-awake 模式">
          <a-select :data-testid="T.wxKeepawake" :value="store.wechatModule.keepawake ?? 'powercfg'"
                    :options="[{ value: 'powercfg', label: 'powercfg(默认)' }, { value: 'request', label: 'request' }, { value: 'off', label: 'off' }]"
                    @change="confirmKeepAwake" />
          <div class="qt-small qt-warn" :data-testid="T.wxKeepawakePowercfgNote">
            powercfg:将修改本机电源计划(台式机场景),待机与关屏超时改为「从不」,关闭微信模块或卸载时按备份还原。
            request 仅发电源请求、不改电源计划(笔记本可选)。锁屏由系统策略决定,只检测告知,不模拟输入绕过。
          </div>
        </a-form-item>
      </a-form>
      <a-button type="primary" :disabled="!session.winagentOnline" :data-testid="T.wxSave"
                @click="store.saveWechatModule(store.wechatModule)">保存</a-button>
    </section>

    <!-- WSL(WinAgent) -->
    <section class="qt-card box">
      <div class="qt-section-title">WSL(.wslconfig 我方键)</div>
      <div v-if="store.wslPendingRestart" class="warnbar" :data-testid="T.wslPendingBanner">待重启 WSL 生效</div>
      <a-form layout="vertical">
        <a-form-item label="memory(GB)">
          <a-input-number :data-testid="T.wsl('memory')" :value="store.wslConfig.memory as number"
                          :disabled="session.wslGroupDisabled"
                          @change="(v: any) => store.wslConfig.memory = v" />
        </a-form-item>
        <a-form-item label="processors(高级,默认不写)">
          <a-input-number :data-testid="T.wsl('processors')" :value="store.wslConfig.processors as number"
                          :disabled="session.wslGroupDisabled"
                          @change="(v: any) => store.wslConfig.processors = v" />
        </a-form-item>
        <a-form-item label="swap(GB)">
          <a-input-number :data-testid="T.wsl('swap')" :value="store.wslConfig.swap as number"
                          :disabled="session.wslGroupDisabled"
                          @change="(v: any) => store.wslConfig.swap = v" />
        </a-form-item>
      </a-form>
      <a-button type="primary" :disabled="session.wslGroupDisabled" :data-testid="T.wslSave"
                @click="store.saveWslConfig(store.wslConfig)">保存</a-button>
    </section>

    <!-- 资源池 / 内存保护 -->
    <section class="qt-card box">
      <div class="qt-section-title">资源池 / 内存保护</div>
      <a-form layout="inline">
        <!-- 🔴 三通道预算在 `quota_mb.*`、WSL 基础占用在 `pools.wsl.reserved_mb`(#88 的 resources 两键形状) -->
        <a-form-item label="企点 MB">
          <a-input-number :data-testid="T.resQuota('qidian')" :value="resQuota.qidian"
                          @change="(v: any) => setQuota('qidian', v)" />
        </a-form-item>
        <a-form-item label="QQ MB">
          <a-input-number :data-testid="T.resQuota('qq')" :value="resQuota.qq"
                          @change="(v: any) => setQuota('qq', v)" />
        </a-form-item>
        <a-form-item label="微信 MB">
          <a-input-number :data-testid="T.resQuota('wechat')" :value="resQuota.wechat"
                          @change="(v: any) => setQuota('wechat', v)" />
        </a-form-item>
        <a-form-item label="WSL 基础 MB">
          <a-input-number :data-testid="T.resQuota('base')" :value="resPools.wsl?.reserved_mb"
                          @change="(v: any) => setPoolReserved('wsl', v)" />
        </a-form-item>
      </a-form>
      <!-- 🔴 内存水位阈值是 `pool` 组的键(07 §2 `[pool]`),与上面的资源池分开保存 -->
      <a-form layout="inline">
        <a-form-item label="mem_warn_mb">
          <a-input-number :data-testid="T.resMem('warn')" :value="poolGroup.mem_warn_mb as number"
                          @change="(v: any) => setMemLevel('mem_warn_mb', v)" />
        </a-form-item>
        <a-form-item label="mem_critical_mb">
          <a-input-number :data-testid="T.resMem('critical')" :value="poolGroup.mem_critical_mb as number"
                          @change="(v: any) => setMemLevel('mem_critical_mb', v)" />
        </a-form-item>
        <a-form-item>
          <a-button @click="saveMemLevels">保存内存水位</a-button>
          <span v-if="store.isPendingRestart('pool')" class="qt-small qt-warn">已保存,重启 Agent 后生效</span>
        </a-form-item>
      </a-form>
      <p class="qt-small qt-muted" :data-testid="T.autostopNote">
        内存吃紧只在 P-RES 建议停用,不自动动你的账号;要让某个账号在内存 critical 时被自动停,
        去该账号详情页开 auto_stop_on_pressure。
      </p>
      <div class="qt-row">
        <a-button :data-testid="T.resCalibrate" @click="calibrate">自校准</a-button>
        <a-button type="primary" :data-testid="T.resSave" @click="saveResources">保存</a-button>
        <span v-if="store.isPendingRestart('resources')" class="qt-small qt-warn">已保存,重启 Agent 后生效</span>
      </div>
    </section>

    <!-- 公网端点 -->
    <section class="qt-card box">
      <div class="qt-section-title">公网端点</div>
      <div class="qt-row wrap">
        <!-- 键集按 02 #102(无 matches/history:见 api/types.ts 的说明) -->
        <span :data-testid="T.pubep('ip')">出口 IP <b>{{ store.publicEndpoint?.public_ip ?? '—' }}</b></span>
        <span :data-testid="T.pubep('dns')">
          出口探测
          <b>{{ (store.publicEndpoint?.probe?.unreachable_rounds ?? 0) > 0
            ? `连续不可达 ${store.publicEndpoint?.probe?.unreachable_rounds} 轮` : '正常' }}</b>
        </span>
        <span :data-testid="T.pubep('changed')">最近变更 <b>{{ store.publicEndpoint?.changed_at ?? '—' }}</b></span>
      </div>
      <div class="qt-row">
        <span :data-testid="T.pubep('host')">配置域名</span>
        <a-input class="w200" :data-testid="T.pubepHostEdit"
                 :value="pubDomainDraft"
                 @change="(e: any) => pubDomainDraft = e.target.value" />
        <a-button size="small" :data-testid="T.pubepHostSave" @click="savePubHost">保存</a-button>
      </div>
      <a-collapse>
        <!-- 历史来自 NET_PUBLIC_ENDPOINT_CHANGED 事件(与 P-ENV 同一数据源) -->
        <a-collapse-panel key="h" header="历史" :data-testid="T.pubepHistory">
          <div v-for="(h, i) in env.endpointHistory" :key="i" class="qt-small"
               :data-testid="T.pubepHistoryRow(i)">{{ h.at }} {{ h.from_ip }} → {{ h.to_ip }}</div>
          <div v-if="!env.endpointHistory.length" class="qt-small qt-muted">本次会话内未收到出口变更事件。</div>
        </a-collapse-panel>
      </a-collapse>
      <div class="qt-section-title mt">webhook 登记</div>
      <div v-for="(w, i) in store.webhooks" :key="w.id" class="qt-row" :data-testid="T.pubepWebhook(i)">
        <span class="qt-grow qt-mono qt-small">{{ w.url }}</span>
        <a-button size="small" danger :data-testid="T.pubepWebhookRemove" @click="removeWebhook(w.id)">删除</a-button>
      </div>
      <a-button size="small" :data-testid="T.pubepWebhookAdd" @click="addWebhook">新增 webhook</a-button>
      <p class="qt-small qt-muted" :data-testid="T.pubepHint">
        本机 API 不绑 IP;公网入站请在使用方侧用域名 + DDNS/反代,IP 变了只需改解析。
      </p>
    </section>

    <!-- 合规 / 关于 / 控制台 -->
    <section class="qt-card box">
      <div class="qt-section-title">合规</div>
      <!--
        🔴 数据源 = #86 `GET /system/notice`(`/settings/compliance` 不在 #88 的 group 枚举里)。
        确认时间优先取 `acked_at`(ISO,00 §6「时间一律 ISO 8601 带时区偏移」),没有才回落毫秒。
      -->
      <div :data-testid="T.compliance('version')">告知版本 {{ notice?.notice_version || '—' }}</div>
      <div :data-testid="T.compliance('ack')">
        确认时间 {{ notice?.acked_at ?? (notice?.ack_ms ? new Date(notice.ack_ms).toLocaleString('zh-CN') : '—') }}
      </div>
      <a-button :data-testid="T.rerunSetup" @click="rerunSetup">重新运行向导</a-button>
    </section>

    <section class="qt-card box">
      <div class="qt-section-title">关于 / 升级</div>
      <div class="qt-row wrap">
        <span :data-testid="T.about('console')">控制台 {{ session.appVersion?.console ?? '—' }}</span>
        <span :data-testid="T.about('agent')">Agent {{ version?.agent ?? '—' }}</span>
        <span :data-testid="T.about('winagent')">
          WinAgent {{ version?.winagent?.version ?? '—' }}(会话代理 {{ version?.winagent?.user_agent ? '在线' : '不在线' }})
        </span>
        <span :data-testid="T.about('schema')">Agent 库 schema v{{ version?.schema_version ?? '—' }}</span>
        <span :data-testid="T.about('wa-schema')">WinAgent 库 schema v{{ version?.wa_schema_version ?? '—' }}</span>
      </div>
      <div :data-testid="T.aboutMigration" :class="{ 'qt-danger': migration.state === 'failed' }">
        迁移状态:{{ ({ idle: '空闲', pending: '待执行', running: '进行中', failed: '失败' })[migration.state] }}
        <template v-if="migration.from !== undefined">(v{{ migration.from }} → v{{ migration.to }},{{ migration.at }})</template>
        <template v-if="migration.state === 'pending' || migration.state === 'running'">—— 升级期间请勿关机</template>
      </div>
      <a-progress
        v-if="migration.state !== 'idle'"
        :data-testid="T.aboutMigrationProgress"
        :percent="migration.state === 'running' ? 50 : migration.state === 'pending' ? 5 : 100"
        :status="migration.state === 'failed' ? 'exception' : 'active'"
        size="small"
      />
      <a-button
        v-if="migration.state === 'failed'"
        danger
        :data-testid="T.aboutMigrationFailedDiag"
        @click="router.push('/env')"
      >去环境页导诊断包</a-button>
      <p class="qt-small qt-muted" :data-testid="T.aboutUpgradeNote">
        升级不会清除登录信息与数据(账号、登录态、消息库、保险库、微信档案全部保留);
        只有重大表结构变化才需要你导出/清库,且升级前会明示并二次确认 —— 加字段不算。
      </p>
    </section>

    <section class="qt-card box">
      <div class="qt-section-title">控制台</div>
      <a-form layout="inline">
        <a-form-item label="主题(深色 M5)">
          <a-select :data-testid="T.console('theme')" :value="ui.theme" :disabled="true"
                    :options="[{ value: 'light', label: '浅色' }, { value: 'dark', label: '深色(M5)' }, { value: 'system', label: '跟随系统(M5)' }]" />
        </a-form-item>
        <a-form-item label="开机自启">
          <a-switch :data-testid="T.console('autolaunch')" :checked="ui.autoLaunch"
                    @change="(v: any) => setAutoLaunch(!!v)" />
        </a-form-item>
        <a-form-item label="关窗口最小化到托盘">
          <a-switch :data-testid="T.console('tray')" :checked="ui.trayOnClose"
                    @change="(v: any) => { ui.trayOnClose = !!v; ui.persist({ app: { minimize_to_tray_on_close: !!v } }) }" />
        </a-form-item>
        <a-form-item label="通知">
          <a-switch :data-testid="T.console('notify')" :checked="ui.notifyEnabled"
                    @change="(v: any) => { ui.notifyEnabled = !!v; ui.persist({ notify: { enabled: !!v } }) }" />
        </a-form-item>
        <a-form-item label="画面默认性能档">
          <a-select :data-testid="T.console('perf')" :value="ui.perfProfile"
                    :options="[{ value: 'focus30', label: '720p@30' }, { value: 'focus15', label: '720p@15' }, { value: 'thumb10', label: '540p@10' }]"
                    @change="(v: any) => { ui.perfProfile = v; ui.persist({ screen: { default_focus_profile: v } }) }" />
        </a-form-item>
      </a-form>
    </section>

    <!-- 明文只显示一次:关闭即从内存清除(afterClose) -->
    <a-modal
      v-model:open="onceModal.open"
      :title="onceModal.title"
      :footer="null"
      :data-testid="T.clientOnceModal"
      @after-close="clearOnce"
    >
      <template v-if="onceModal.missing">
        <p class="qt-danger">
          本次<strong>没有拿到明文</strong> —— 后端这一版的响应里没有 token/secret。
          该凭据已创建但无法使用,请吊销后重建;若反复如此请把 trace 交给后端排查。
        </p>
      </template>
      <template v-else>
        <p class="qt-danger">明文只显示这一次,<strong>关闭后无法再次查看</strong>(不落盘、不进日志)。</p>
        <a-input class="qt-mono" :value="onceModal.value" readonly />
        <div class="qt-row mt">
          <a-button type="primary" :data-testid="T.clientOnceCopy" @click="copyText(onceModal.value)">
            复制
          </a-button>
          <span :data-testid="T.mailKeyOnceModal" class="hidden" />
          <a-button :data-testid="T.mailKeyOnceCopy" @click="copyText(onceModal.value)">复制密钥</a-button>
        </div>
      </template>
    </a-modal>

    <a-modal
      v-model:open="vaultModal.open"
      title="更新保险库条目"
      :data-testid="T.vaultRowUpdateModal"
      :footer="null"
    >
      <p class="qt-small qt-muted">主进程会先弹原生确认框,确认后才转发到 WinAgent。响应不回显。</p>
      <a-input v-model:value="vaultModal.secret" type="password" autocomplete="new-password" placeholder="新值" />
      <div class="qt-row mt">
        <a-button @click="vaultModal.open = false">取消</a-button>
        <a-button type="primary" :data-testid="T.vaultRowUpdateSubmit" @click="submitVault">保存</a-button>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.box { padding: var(--qt-space-4); }
.tbl { width: 100%; border-collapse: collapse; margin-bottom: var(--qt-space-2); }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.scope { padding: var(--qt-space-3); margin: var(--qt-space-2) 0; }
.fields { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--qt-space-2); }
.field { display: flex; flex-direction: column; gap: 2px; }
.sender { margin-bottom: 6px; flex-wrap: wrap; }
.w160 { width: 160px; } .mb { margin-bottom: var(--qt-space-3); }
.w200 { width: 200px; }
.mb { margin-bottom: var(--qt-space-3); }
.ops { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }
.opitem { display: flex; align-items: center; gap: 6px; }
.danger-tag { color: var(--qt-state-error); border: 1px solid var(--qt-state-error); border-radius: 8px; padding: 0 4px; font-size: var(--qt-font-xs); }
.badge { color: var(--qt-state-error); border: 1px solid var(--qt-state-error); border-radius: 8px; padding: 0 6px; font-size: var(--qt-font-xs); }
.warnbar { background: #FFFBE6; color: var(--qt-sev-warn); padding: 6px var(--qt-space-3); margin-bottom: var(--qt-space-2); }
.wrap { flex-wrap: wrap; gap: var(--qt-space-3); }
.mt { margin-top: var(--qt-space-3); }
.mini { max-height: 120px; overflow: auto; font-size: var(--qt-font-xs); background: var(--qt-bg); padding: 6px; }
.hidden { display: none; }
</style>
