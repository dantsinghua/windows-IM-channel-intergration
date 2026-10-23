<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ossboxApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import type { OssboxAuthenticationResult, OssboxSettingsInput, OssboxSettingsResponse } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const emit = defineEmits<{ saved: [] }>()
const session = useSessionStore()
const saved = ref<OssboxSettingsResponse | null>(null)
const institution = computed(() => saved.value?.institution_runtime)
let statusTimer: ReturnType<typeof setInterval> | undefined
let statusRequest: AbortController | undefined
const statusError = ref('')
const now = ref(Date.now() / 1000)
async function refreshRuntime() {
  if (!alive || !institution.value || busy.value || statusRequest || session.gateVisible) return
  const previous = saved.value
  const controller = new AbortController(); statusRequest = controller
  const deadline = setTimeout(() => controller.abort(), 10000)
  try {
    const result = await ossboxApi.get(controller.signal)
    if (alive && !controller.signal.aborted && !session.gateVisible && saved.value === previous && saved.value && result.relay.workbench_id === saved.value.relay.workbench_id) {
      saved.value = { ...saved.value, state: result.state, applied: result.applied, restart_required: result.restart_required, institution_runtime: result.institution_runtime }
      statusError.value = ''
    }
  } catch { if (alive) statusError.value = '运行状态读取失败；不能据旧缓存判断已上报。' }
  finally { clearTimeout(deadline); if (statusRequest === controller) statusRequest = undefined }
}
const tlsFields = ['client_cert_file', 'client_key_file', 'ca_file'] as const
const relayForm = reactive({ base_url: '', workbench_name: '', key: '', sm4_key: '', client_cert_file: '', client_key_file: '', ca_file: '' })
const loaded = ref(false)
const busy = ref<'' | 'load' | 'save' | 'test'>('')
const note = ref('')
const authenticated = ref<OssboxAuthenticationResult | null>(null)
const locked = computed(() => !loaded.value || session.gateVisible || session.draining || busy.value === 'save')
const missingWorkbenchName = computed(() => !relayForm.workbench_name.trim())
let revision = 0
let alive = true
let activeRequest: AbortController | null = null
let activeTimer: ReturnType<typeof setTimeout> | null = null

watch(relayForm, () => {
  revision += 1
  authenticated.value = null
  note.value = ''
}, { deep: true, flush: 'sync' })

function errorText(error: unknown, operation: string): string {
  // 不显示服务端原始报错或请求内容，防止上游错误响应意外带回口令。
  if (error instanceof ApiFailure && error.status === 403) return '仅管理员可以配置和测试认证。'
  if (error instanceof ApiFailure && error.status === 401) return '管理会话已失效，请恢复连接后重试。'
  const reasons: Record<string, string> = {
    collector_workbench_name_invalid: '请填写 1 至 128 个字符的工作台名称。',
    file_tls_client_cert_required: '正式环境必须配置客户端证书与私钥路径。',
    file_tls_cert_pair_required: '客户端证书与私钥必须同时配置。',
    file_tls_file_unavailable: '证书文件不存在或不可读取，请核对服务端路径。',
    file_tls_client_cert_invalid: '证书与私钥无效、不匹配或私钥需要解密。',
    file_tls_server_cert_invalid: '服务器证书验证失败，请检查系统信任库或 CA 配置。',
    file_tls_handshake_failed: '双向 TLS 握手失败，请核对证书与生产环境。',
    collector_url_invalid: '统一服务地址格式不正确，请检查地址。',
    collector_configuration_invalid: '配置格式不正确，请检查工作台名称、地址、key 与 sm4_key。',
    collector_credentials_missing: '尚未配置公网认证，请填写 key 与 sm4_key。',
    collector_save_failed: '采集端配置保存失败，请检查服务连接后重试。',
    vault_unavailable: '安全凭据存储暂不可用，请检查服务连接后重试。',
    collector_test_unavailable: '认证测试暂不可用，请检查服务连接后重试。',
    collector_test_timeout: '认证测试请求超时，请检查服务连通性。',
    file_auth_rejected: '认证被拒绝，请核对 key、sm4_key 与部署环境。',
    file_auth_key_invalid: 'key 格式不正确，请检查业务口令。',
    file_auth_sm4_length_invalid: 'sm4_key 必须恰好为 16 个 UTF-8 字节。',
    file_auth_config_invalid: '认证配置格式不正确，请检查工作台名称、地址、key 与 sm4_key。',
    file_gateway_unavailable: '公网服务暂不可用，请检查服务连接后重试。',
  }
  if (error instanceof ApiFailure && error.reason && Object.prototype.hasOwnProperty.call(reasons, error.reason)) {
    return reasons[error.reason]!
  }
  if (error instanceof ApiFailure && error.status === 400) return '配置格式不正确，请检查工作台名称、地址、key 与 sm4_key。'
  return `${operation}失败，请检查配置或服务连接后重试。`
}

function finishRequest(controller: AbortController): void {
  if (activeRequest !== controller) return
  if (activeTimer) clearTimeout(activeTimer)
  activeTimer = null
  activeRequest = null
  busy.value = ''
}

function setRequestTimeout(controller: AbortController, ms: number, submittedRevision?: number): void {
  activeTimer = setTimeout(() => {
    if (!alive || activeRequest !== controller) return
    const operation = busy.value
    controller.abort()
    finishRequest(controller)
    if (operation === 'save') loaded.value = false
    if (submittedRevision === undefined || submittedRevision === revision) {
      note.value = operation === 'save'
        ? '保存请求超时，结果待核实；请重新读取配置后再操作。'
        : `${operation === 'load' ? '读取配置' : '认证测试'}超时，请检查服务连接后重试。`
    }
  }, ms)
}

function requestBody(): OssboxSettingsInput | null {
  const workbenchName = relayForm.workbench_name.trim()
  if (!workbenchName || Array.from(workbenchName).length > 128) {
    note.value = '请填写 1 至 128 个字符的工作台名称。'
    return null
  }
  const baseUrl = relayForm.base_url.trim()
  try {
    const url = new URL(baseUrl)
    if (!/^https:\/\/[^/?#\s]+\/?$/i.test(baseUrl) || url.protocol !== 'https:' || url.pathname !== '/' || url.username || url.password) throw new Error()
  } catch {
    note.value = '请填写有效的 HTTPS 根地址，不要携带路径、用户名、密码、查询参数或片段。'
    return null
  }
  const body: OssboxSettingsInput = { base_url: baseUrl, workbench_name: workbenchName }
  for (const key of tlsFields) {
    if (relayForm[key] || saved.value?.relay[key]) body[key] = relayForm[key]
  }
  if (!relayForm.key && !saved.value?.relay.key_configured) {
    note.value = '请填写 key。'
    return null
  }
  if (!relayForm.sm4_key && !saved.value?.relay.sm4_key_configured) {
    note.value = '请填写 sm4_key。'
    return null
  }
  if (relayForm.key && new TextEncoder().encode(relayForm.key).length > 4096) {
    note.value = 'key 的 UTF-8 长度不能超过 4096 字节。'
    return null
  }
  if (relayForm.sm4_key && new TextEncoder().encode(relayForm.sm4_key).length !== 16) {
    note.value = 'sm4_key 必须恰好为 16 个 UTF-8 字节；中文等字符通常占多个字节。'
    return null
  }
  // 不 trim 密钥；空输入表示保留已保存值，不发送占位符或空串。
  if (relayForm.key) body.key = relayForm.key
  if (relayForm.sm4_key) body.sm4_key = relayForm.sm4_key
  return body
}

async function loadSettings(): Promise<void> {
  if (!alive || busy.value || session.gateVisible) return
  const controller = new AbortController()
  activeRequest = controller
  busy.value = 'load'
  note.value = ''
  setRequestTimeout(controller, 20_000)
  try {
    const result = await ossboxApi.get(controller.signal)
    if (!alive || activeRequest !== controller || controller.signal.aborted) return
    // Apply the complete form only after a successful, current read.
    saved.value = result
    relayForm.base_url = result.relay.base_url
    relayForm.workbench_name = result.relay.workbench_name
    for (const key of tlsFields) relayForm[key] = result.relay[key] ?? ''
    relayForm.key = ''
    relayForm.sm4_key = ''
    // GET 同样受后端 admin 权限保护，成功后才开放配置操作。
    loaded.value = true
  } catch (error) {
    if (alive && activeRequest === controller) note.value = errorText(error, '读取配置')
  } finally {
    finishRequest(controller)
  }
}

async function submit(operation: 'save' | 'test'): Promise<void> {
  if (!alive || locked.value || busy.value) return
  authenticated.value = null
  note.value = ''
  const body = requestBody()
  if (!body) return
  const submittedRevision = revision
  const controller = new AbortController()
  activeRequest = controller
  busy.value = operation
  setRequestTimeout(controller, 45_000, submittedRevision)
  try {
    if (operation === 'test') {
      const result = await ossboxApi.test(body, controller.signal)
      if (!alive || activeRequest !== controller || controller.signal.aborted || revision !== submittedRevision) return
      if (result.authenticated !== true) throw new Error('Authentication not confirmed')
      authenticated.value = result
    } else {
      const result = await ossboxApi.put(body, controller.signal)
      if (!alive || activeRequest !== controller || controller.signal.aborted) return
      saved.value = result
      const relayNote = result.institution_runtime
        ? (result.applied ? '采集端配置已装载，文件同步和原生回执需分别核验。' : '配置已保存，采集服务尚未应用该版本。')
        : '采集端配置已保存，待对接。'
      if (revision !== submittedRevision) {
        note.value = `${relayNote}页面上的后续修改尚未保存。`
        return
      }
      relayForm.base_url = result.relay.base_url
      relayForm.workbench_name = result.relay.workbench_name
      for (const key of tlsFields) relayForm[key] = result.relay[key] ?? ''
      relayForm.key = ''
      relayForm.sm4_key = ''
      note.value = relayNote
      emit('saved')
    }
  } catch (error) {
    if (alive && activeRequest === controller && revision === submittedRevision) {
      const rejected = error instanceof ApiFailure && [400, 401, 403, 404, 409, 422, 429].includes(error.status)
      if (operation === 'save' && !rejected) {
        loaded.value = false
        note.value = '保存结果待核实；请重新读取配置后再操作。'
      } else {
        if (error instanceof ApiFailure && [401, 403].includes(error.status)) loaded.value = false
        note.value = errorText(error, operation === 'test' ? '认证测试' : '保存配置')
      }
    }
  } finally {
    finishRequest(controller)
  }
}

watch(() => session.gateVisible, (gated) => {
  if (!gated) { if (!loaded.value) void loadSettings(); return }
  statusRequest?.abort()
  activeRequest?.abort()
  if (activeRequest) finishRequest(activeRequest)
  loaded.value = false
  relayForm.key = ''
  relayForm.sm4_key = ''
  authenticated.value = null
})

onMounted(() => { void loadSettings(); let tick = 0; statusTimer = setInterval(() => { now.value = Date.now() / 1000; if (++tick % 10 === 0) void refreshRuntime() }, 1000) })
onUnmounted(() => {
  alive = false
  if (statusTimer) clearInterval(statusTimer)
  statusRequest?.abort()
  activeRequest?.abort()
  if (activeRequest) finishRequest(activeRequest)
  relayForm.key = ''
  relayForm.sm4_key = ''
})
</script>

<template>
  <article class="ossbox-card link-card qt-glass" data-testid="ossbox-config">
    <div class="qt-eyebrow">COLLECT</div>
    <h2>公网上报</h2>
    <p v-if="institution">工作台名称和机构凭据由本机保存；账号路径由系统内置，不提供上下行编辑。认证查询、文件上报和原生消息送达是不同状态。</p>
    <p v-else>填写工作台名称、统一服务地址及 key、sm4_key。当前仅保存采集端配置，待与对端对齐；认证测试只查询待处理数量，不保存草稿、不上传消息。</p>
    <p v-if="busy === 'load'" role="status">正在读取配置…</p>
    <form autocomplete="off" @submit.prevent="submit('save')">
      <fieldset :disabled="locked">
        <label>工作台名称<input v-model="relayForm.workbench_name" data-testid="ossbox-workbench-name" autocomplete="off" placeholder="填写工作台名称" /></label>
        <label>统一服务地址<input v-model="relayForm.base_url" data-testid="ossbox-relay-url" type="url" autocomplete="off" placeholder="部署方提供的统一服务地址" /></label>
        <label>客户端证书路径（服务端 .crt）<input v-model="relayForm.client_cert_file" data-testid="relay-client-cert" autocomplete="off" /></label>
        <label>客户端私钥路径（服务端 .key）<input v-model="relayForm.client_key_file" data-testid="relay-client-key" autocomplete="off" /></label>
        <label>服务器 CA 路径（可选）<input v-model="relayForm.ca_file" data-testid="relay-ca-file" autocomplete="off" placeholder="留空使用系统信任库" /></label>
        <p>正式地址：https://its-relay.yingmi.cn。证书路径由运行采集服务的机器解析；只保存路径，不向页面返回私钥内容。</p>
        <p v-if="saved?.relay?.workbench_id" data-testid="ossbox-workbench-id">稳定标识：{{ saved.relay.workbench_id }}</p>
        <label>业务口令（key）<input v-model="relayForm.key" data-testid="ossbox-relay-key" type="password" autocomplete="new-password" :placeholder="saved?.relay?.key_configured ? '已配置；留空保留，输入新值替换' : '填写部署方提供的 key'" /></label>
        <span class="secret-state">{{ saved?.relay?.key_configured ? (relayForm.key ? '已有保存口令；本次输入尚未保存' : '业务口令已配置') : (relayForm.key ? '本次已输入，尚未保存' : '业务口令未配置') }}</span>
        <label>SM4 密钥（sm4_key）<input v-model="relayForm.sm4_key" data-testid="ossbox-relay-sm4-key" type="password" autocomplete="new-password" :placeholder="saved?.relay?.sm4_key_configured ? '已配置；留空保留，输入新值替换' : 'UTF-8 编码后必须为 16 字节'" /></label>
        <span class="secret-state">{{ saved?.relay?.sm4_key_configured ? (relayForm.sm4_key ? '已有保存密钥；本次输入尚未保存' : 'SM4 密钥已配置') : (relayForm.sm4_key ? '本次已输入，尚未保存' : 'SM4 密钥未配置') }}</span>
      </fieldset>
      <div class="qt-row wrap">
        <a-button type="primary" html-type="submit" data-testid="ossbox-save" :disabled="locked || !!busy || missingWorkbenchName" :loading="busy === 'save'">保存公网上报</a-button>
        <a-button data-testid="ossbox-test" :disabled="locked || !!busy || missingWorkbenchName" :loading="busy === 'test'" @click="submit('test')">测试公网认证</a-button>
        <a-button v-if="!loaded && !busy" :disabled="session.gateVisible" @click="loadSettings">重新读取</a-button>
      </div>
    </form>
    <p v-if="note" role="status" data-testid="ossbox-note">{{ note }}</p>
    <p v-if="authenticated" class="auth-result" role="status" data-testid="ossbox-auth-result">公网认证通过，仅验证待处理查询。{{ Number.isSafeInteger(authenticated.pending) && authenticated.pending! >= 0 ? `待处理：${authenticated.pending}。` : '' }}{{ institution ? '此结果不代表文件上传或原生消息送达。' : '采集端仍待对接，尚未发布目录；此结果不代表上传或消息链路已通过。' }}</p>
    <template v-if="saved && institution">
      <p data-testid="ossbox-save-status">{{ saved.applied ? '配置已由采集服务装载。' : '配置尚未由采集服务应用。' }}{{ saved.restart_required ? '保存版本与运行版本不同；需完成受控切换。' : '' }}</p>
      <section data-testid="institution-reporting-status">
        <p>机构文件通道：{{ institution.active ? '已运行' : '已停止' }} · 采集账号工作进程 {{ institution.account_workers }}</p>
        <p>最近目录上传：{{ institution.last_directory_at ? new Date(institution.last_directory_at * 1000).toLocaleString() : '尚无成功记录' }}。上传完成不代表内网已接收。</p>
        <p v-if="statusError" role="alert">{{ statusError }}</p>
        <p v-for="(code, component) in institution.errors" :key="component" role="alert">{{ component }}：{{ code }}</p>
        <p v-for="(presence, uid) in institution.native_presence" :key="uid">{{ presence.channel === 'wechat' ? '微信' : '企点' }} {{ presence.account_uid || uid }}：{{ presence.state === 'online' && now - presence.confirmed_at < 60 ? '原生在线已核验' : presence.state === 'offline' ? '原生已离线' : '原生状态尚未确认' }} · {{ presence.code }}</p>
        <p v-for="(history, uid) in institution.history" :key="uid">企点 {{ uid }} 主库历史：读取 {{ history.scanned }}，提交 {{ history.published }}，附件 {{ history.media_bytes }} 字节{{ history.error ? '；'+history.error : '' }}。提交数量不等于内网入库数量。</p>
        <a-button data-testid="institution-refresh-status" :disabled="!!busy || session.gateVisible" @click="refreshRuntime">刷新运行状态</a-button>
      </section>
    </template>
    <template v-else-if="saved">
      <p data-testid="ossbox-save-status">{{ saved.relay.saved ? (saved.relay.credential_error ? '已有保存配置，但认证凭据暂不可用，请检查服务连接。' : '采集端配置已保存，待对接。') : '尚未保存配置。' }}当前状态：待对齐，尚未发布新目录，现有连接未切换。</p>
    </template>
  </article>
</template>

<style scoped>
.ossbox-card { min-width: 0; }
h2 { margin: 0; font-size: 20px; font-weight: 600; }
p { margin: 0; color: var(--qt-text-secondary); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
form, fieldset { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
fieldset { border: 0; margin: 0; padding: 0; }
label { display: flex; flex-direction: column; gap: 6px; color: var(--qt-text-secondary); font-size: 12px; }
input { box-sizing: border-box; width: 100%; padding: 8px 12px; border: 1px solid var(--qt-border); border-radius: 9px; background: white; color: var(--qt-text); }
input:disabled { opacity: .65; }
.secret-state { color: var(--qt-text-secondary); font-size: 12px; }
.auth-result { color: var(--qt-primary); }
</style>
