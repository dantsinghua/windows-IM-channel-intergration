<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ossboxApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import type { ItsRelayCredentialsInput, ItsRelaySettingsInput, OssboxAuthenticationResult, OssboxLegacySettingsInput, OssboxSettingsInput, OssboxSettingsResponse } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const emit = defineEmits<{ saved: [] }>()
const session = useSessionStore()
const form = reactive({
  sts_url: '', auth_key: '', sm4_key: '', allow_http: false,
  endpoint: '', bucket: '', workbench_id: '',
})
const saved = ref<OssboxSettingsResponse | null>(null)
const editingContract = ref<'legacy' | 'collector' | undefined>()
const relayMode = computed(() => saved.value?.mode === 'its_relay')
const legacyRelayMode = computed(() => relayMode.value && saved.value?.relay?.config_file === 'config.yaml' && saved.value?.relay?.workbench_name === undefined)
const institution = computed(() => saved.value?.institution_runtime)
let statusTimer: ReturnType<typeof setInterval> | undefined
let statusRequest: AbortController | undefined
const statusError = ref('')
const now = ref(Date.now() / 1000)
async function refreshRuntime() {
  if (!alive || !institution.value || busy.value || statusRequest || session.gateVisible) return
  const controller = new AbortController(); statusRequest = controller
  const deadline = setTimeout(() => controller.abort(), 10000)
  try {
    const result = editingContract.value
      ? await ossboxApi.getForContract(editingContract.value, controller.signal)
      : await ossboxApi.get(controller.signal)
    if (alive && !controller.signal.aborted && saved.value && result.relay?.workbench_id === saved.value.relay?.workbench_id) {
      saved.value = { ...saved.value, state: result.state, applied: result.applied, restart_required: result.restart_required, institution_runtime: result.institution_runtime }
      statusError.value = ''
    }
  } catch { if (alive) statusError.value = '运行状态读取失败；不能据旧缓存判断已上报。' }
  finally { clearTimeout(deadline); if (statusRequest === controller) statusRequest = undefined }
}
const relayForm = reactive({ base_url: '', workbench_name: '', key: '', sm4_key: '' })
const loaded = ref(false)
const busy = ref<'' | 'load' | 'save' | 'test'>('')
const note = ref('')
const authenticated = ref<OssboxAuthenticationResult | null>(null)
const locked = computed(() => !loaded.value || session.gateVisible || session.draining || busy.value === 'save')
const missingWorkbenchName = computed(() => relayMode.value && !legacyRelayMode.value && !relayForm.workbench_name.trim())
let revision = 0
let alive = true
let activeRequest: AbortController | null = null
let activeTimer: ReturnType<typeof setTimeout> | null = null

watch([form, relayForm], () => {
  revision += 1
  authenticated.value = null
  note.value = ''
}, { deep: true, flush: 'sync' })

function errorText(error: unknown, operation: string): string {
  // 不显示服务端原始报错或请求内容，防止上游错误响应意外带回口令。
  if (error instanceof ApiFailure && error.status === 403) return '仅管理员可以配置和测试认证。'
  if (error instanceof ApiFailure && error.status === 401) return '管理会话已失效，请恢复连接后重试。'
  if (relayMode.value) {
    const reasons: Record<string, string> = {
      collector_workbench_name_invalid: '请填写 1 至 128 个字符的工作台名称。',
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
      relay_config_invalid: '配置格式不正确，请检查工作台名称、地址、key 与 sm4_key。',
      relay_not_configured: '尚未配置公网认证，请填写 key 与 sm4_key。',
      relay_save_failed: '配置保存失败，请检查服务连接后重试。',
      relay_test_timeout: '认证测试请求超时，请检查服务连通性。',
      relay_test_unavailable: '认证测试暂不可用，请检查服务连接后重试。',
      file_gateway_unavailable: '公网服务暂不可用，请检查服务连接后重试。',
    }
    if (error instanceof ApiFailure && error.reason && Object.prototype.hasOwnProperty.call(reasons, error.reason)) {
      return reasons[error.reason]!
    }
    if (error instanceof ApiFailure && error.status === 400) return '配置格式不正确，请检查工作台名称、地址、key 与 sm4_key。'
    return `${operation}失败，请检查配置或服务连接后重试。`
  }
  // Only display reviewed backend reason codes, never the upstream response or secret text.
  if (error instanceof ApiFailure && error.reason === 'sts_http_401')
    return 'STS 返回 401：请核对业务口令、SM4 密钥、客户端时间及目标 STS 环境。'
  if (error instanceof ApiFailure && error.reason === 'sts_unavailable')
    return 'Agent 无法连接 STS 服务。请检查 WSL/VPN 网络、DNS 和 STS 地址。'
  if (error instanceof ApiFailure && error.reason === 'sts_test_timeout')
    return 'STS 认证请求超时，请检查服务连通性和响应时间。'
  if (error instanceof ApiFailure && error.reason === 'sts_response_invalid')
    return 'STS 响应中的临时凭据字段不符合协议合同，需检查 STS 服务版本。'
  if (error instanceof ApiFailure && error.status === 400) return '配置格式不正确，请检查地址、业务口令与 SM4 密钥。'
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
  if (relayMode.value) {
    const body: ItsRelayCredentialsInput | ItsRelaySettingsInput = {}
    if (!legacyRelayMode.value) {
      const workbenchName = relayForm.workbench_name.trim()
      if (!workbenchName || Array.from(workbenchName).length > 128) {
        note.value = '请填写 1 至 128 个字符的工作台名称。'
        return null
      }
      const baseUrl = relayForm.base_url.trim()
      try {
        const url = new URL(baseUrl)
        if (!/^https?:\/\//i.test(baseUrl) || !['https:', 'http:'].includes(url.protocol) || url.username || url.password || /[?#\s]/.test(baseUrl)) throw new Error()
      } catch {
        note.value = '请填写有效的统一服务地址，不要携带用户名、密码、查询参数或片段。'
        return null
      }
      Object.assign(body, { base_url: baseUrl, workbench_name: workbenchName })
    }
    if (!relayForm.key && !saved.value?.relay?.key_configured) {
      note.value = '请填写 key。'
      return null
    }
    if (!relayForm.sm4_key && !saved.value?.relay?.sm4_key_configured) {
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
    if (relayForm.key) body.key = relayForm.key
    if (relayForm.sm4_key) body.sm4_key = relayForm.sm4_key
    return body
  }
  const body: OssboxLegacySettingsInput = {
    sts_url: form.sts_url.trim(), allow_http: form.allow_http,
    endpoint: form.endpoint.trim(), bucket: form.bucket.trim(), workbench_id: form.workbench_id.trim(),
  }
  try {
    const url = new URL(body.sts_url)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || /\s/.test(body.sts_url)) throw new Error()
    if (url.protocol === 'http:' && !body.allow_http) {
      note.value = 'HTTP 地址仅用于测试环境，请显式勾选允许 HTTP。'
      return null
    }
  } catch {
    note.value = '请填写有效的 STS 接口地址，不要在地址中携带用户名、密码或查询参数。'
    return null
  }
  if (body.endpoint) {
    try {
      const endpoint = new URL(body.endpoint.includes('://') ? body.endpoint : `https://${body.endpoint}`)
      if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || endpoint.pathname !== '/' || /\s/.test(body.endpoint)) throw new Error()
    } catch {
      note.value = 'OSS 上报地址须为域名或 HTTPS 地址，不支持 HTTP、路径或认证参数。'
      return null
    }
  }
  if (!form.auth_key && !saved.value?.data.auth_key_configured) {
    note.value = '请填写业务口令（auth.key）。'
    return null
  }
  if (!form.sm4_key && !saved.value?.data.sm4_key_configured) {
    note.value = '请填写 SM4 密钥（auth.sm4_key）。'
    return null
  }
  if (form.auth_key && new TextEncoder().encode(form.auth_key).length > 4096) {
    note.value = '业务口令的 UTF-8 长度不能超过 4096 字节。'
    return null
  }
  if (form.sm4_key && new TextEncoder().encode(form.sm4_key).length !== 16) {
    note.value = 'SM4 密钥必须恰好为 16 个 UTF-8 字节；中文等字符通常占多个字节。'
    return null
  }
  // 不 trim 密钥；空输入表示保留已保存值，不发送占位符或空串。
  if (form.auth_key) body.auth_key = form.auth_key
  if (form.sm4_key) body.sm4_key = form.sm4_key
  return body
}

async function loadSettings(): Promise<void> {
  await readSettings(editingContract.value)
}

async function readSettings(contract: 'legacy' | 'collector' | undefined): Promise<void> {
  if (!alive || busy.value || session.gateVisible) return
  const controller = new AbortController()
  activeRequest = controller
  busy.value = 'load'
  note.value = ''
  setRequestTimeout(controller, 20_000)
  try {
    const result = contract
      ? await ossboxApi.getForContract(contract, controller.signal)
      : await ossboxApi.get(controller.signal)
    if (!alive || activeRequest !== controller || controller.signal.aborted) return
    // Commit the editor target only with its successfully loaded form.
    // Rejected, aborted, and late reads must leave the displayed target intact.
    editingContract.value = contract
    saved.value = result
    if (relayMode.value) {
      relayForm.base_url = result.relay?.base_url ?? ''
      relayForm.workbench_name = result.relay?.workbench_name ?? ''
    } else {
      Object.assign(form, {
        sts_url: result.data.sts_url, allow_http: result.data.allow_http,
        endpoint: result.data.endpoint, bucket: result.data.bucket, workbench_id: result.data.workbench_id,
        auth_key: '', sm4_key: '',
      })
    }
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

async function switchContract(contract: 'legacy' | 'collector') {
  if (locked.value || busy.value) return
  form.auth_key = ''
  form.sm4_key = ''
  relayForm.key = ''
  relayForm.sm4_key = ''
  authenticated.value = null
  await readSettings(contract)
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
      const result = editingContract.value
        ? await ossboxApi.testForContract(editingContract.value, body, controller.signal)
        : await ossboxApi.test(body, controller.signal)
      if (!alive || activeRequest !== controller || controller.signal.aborted || revision !== submittedRevision) return
      if (result.authenticated !== true) throw new Error('Authentication not confirmed')
      authenticated.value = result
    } else {
      const result = editingContract.value
        ? await ossboxApi.putForContract(editingContract.value, body, controller.signal)
        : await ossboxApi.put(body, controller.signal)
      if (!alive || activeRequest !== controller || controller.signal.aborted) return
      saved.value = result
      const relayNote = legacyRelayMode.value
        ? '配置已保存。密钥不回显；变更需受控重启采集服务后生效，页面不会自动重启。'
        : result.institution_runtime ? (result.applied ? '采集端配置已装载，文件同步和原生回执需分别核验。' : '配置已保存，采集服务尚未应用该版本。') : '采集端配置已保存，待对接。'
      if (revision !== submittedRevision) {
        note.value = relayMode.value
          ? `${relayNote}页面上的后续修改尚未保存。`
          : '请求中的配置已保存；页面上的后续修改尚未保存。重启 Agent 后生效。'
        return
      }
      if (relayMode.value) {
        relayForm.base_url = result.relay?.base_url ?? ''
        relayForm.workbench_name = result.relay?.workbench_name ?? ''
      }
      form.auth_key = ''
      form.sm4_key = ''
      relayForm.key = ''
      relayForm.sm4_key = ''
      note.value = relayMode.value
        ? relayNote
        : '配置已保存。密钥不回显；重启 Agent 后生效，页面不会自动重启服务。'
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
  activeRequest?.abort()
  if (activeRequest) finishRequest(activeRequest)
  loaded.value = false
  form.auth_key = ''
  form.sm4_key = ''
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
  form.auth_key = ''
  form.sm4_key = ''
  relayForm.key = ''
  relayForm.sm4_key = ''
})
</script>

<template>
  <article class="ossbox-card link-card qt-glass" data-testid="ossbox-config">
    <div class="qt-eyebrow">COLLECT</div>
    <h2>公网上报</h2>
    <p v-if="relayMode && institution">工作台名称和机构凭据由本机保存；账号路径由系统内置，不提供上下行编辑。认证查询、文件上报和原生消息送达是不同状态。</p>
    <p v-else-if="legacyRelayMode">统一服务地址由部署配置管理；此处仅修改 key 与 sm4_key。认证测试只查询待处理数量，不保存草稿、不上传消息。</p>
    <p v-else-if="relayMode">填写工作台名称、统一服务地址及 key、sm4_key。当前仅保存采集端配置，待与对端对齐；认证测试只查询待处理数量，不保存草稿、不上传消息。</p>
    <p v-else>填写部署方提供的 STS 地址和认证密钥。认证测试只申请临时凭据，不保存草稿、不上传消息，也不启用询价或群消息转发。</p>
    <p v-if="busy === 'load'" role="status">正在读取配置…</p>
    <div v-if="loaded && relayMode && !institution" class="qt-row wrap">
      <a-button v-if="legacyRelayMode" data-testid="reporting-edit-collector" :disabled="locked || !!busy" @click="switchContract('collector')">编辑新工作台配置</a-button>
      <a-button v-else-if="saved?.legacy_available" data-testid="reporting-edit-legacy" :disabled="locked || !!busy" @click="switchContract('legacy')">编辑旧部署认证</a-button>
      <span>切换编辑不切换运行链路；未保存的输入将清空。</span>
    </div>
    <form autocomplete="off" @submit.prevent="submit('save')">
      <fieldset :disabled="locked">
        <template v-if="relayMode">
          <label v-if="!legacyRelayMode">工作台名称<input v-model="relayForm.workbench_name" data-testid="ossbox-workbench-name" autocomplete="off" placeholder="填写工作台名称" /></label>
          <label>统一服务地址<input v-model="relayForm.base_url" data-testid="ossbox-relay-url" :readonly="legacyRelayMode" type="url" autocomplete="off" placeholder="部署方提供的统一服务地址" /></label>
          <p v-if="saved?.relay?.workbench_id" data-testid="ossbox-workbench-id">稳定标识：{{ saved.relay.workbench_id }}</p>
          <label>业务口令（key）<input v-model="relayForm.key" data-testid="ossbox-relay-key" type="password" autocomplete="new-password" :placeholder="saved?.relay?.key_configured ? '已配置；留空保留，输入新值替换' : '填写部署方提供的 key'" /></label>
          <span class="secret-state">{{ saved?.relay?.key_configured ? (relayForm.key ? '已有保存口令；本次输入尚未保存' : '业务口令已配置') : (relayForm.key ? '本次已输入，尚未保存' : '业务口令未配置') }}</span>
          <label>SM4 密钥（sm4_key）<input v-model="relayForm.sm4_key" data-testid="ossbox-relay-sm4-key" type="password" autocomplete="new-password" :placeholder="saved?.relay?.sm4_key_configured ? '已配置；留空保留，输入新值替换' : 'UTF-8 编码后必须为 16 字节'" /></label>
          <span class="secret-state">{{ saved?.relay?.sm4_key_configured ? (relayForm.sm4_key ? '已有保存密钥；本次输入尚未保存' : 'SM4 密钥已配置') : (relayForm.sm4_key ? '本次已输入，尚未保存' : 'SM4 密钥未配置') }}</span>
        </template>
        <template v-else>
        <label>STS 接口地址<input v-model="form.sts_url" data-testid="ossbox-sts-url" type="url" autocomplete="off" placeholder="部署方提供的完整 STS 接口地址" /></label>
        <label class="check"><input v-model="form.allow_http" data-testid="ossbox-allow-http" type="checkbox" />允许 HTTP（仅测试环境，认证请求不加密传输）</label>
        <label>业务口令（auth.key）<input v-model="form.auth_key" data-testid="ossbox-auth-key" type="password" autocomplete="new-password" :placeholder="saved?.data.auth_key_configured ? '已配置；留空保留，输入新值替换' : '填写部署方提供的业务口令'" /></label>
        <span class="secret-state" data-testid="ossbox-auth-status">{{ saved?.data.auth_key_configured ? (form.auth_key ? '已有保存口令；本次输入尚未保存' : '业务口令已配置') : (form.auth_key ? '本次已输入，尚未保存' : '业务口令未配置') }}</span>
        <label>SM4 密钥（auth.sm4_key）<input v-model="form.sm4_key" data-testid="ossbox-sm4-key" type="password" autocomplete="new-password" :placeholder="saved?.data.sm4_key_configured ? '已配置；留空保留，输入新值替换' : 'UTF-8 编码后必须为 16 字节'" /></label>
        <span class="secret-state" data-testid="ossbox-sm4-status">{{ saved?.data.sm4_key_configured ? (form.sm4_key ? '已有保存密钥；本次输入尚未保存' : 'SM4 密钥已配置') : (form.sm4_key ? '本次已输入，尚未保存' : 'SM4 密钥未配置') }}</span>
        <p class="optional-hint">以下业务配置可稍后填写；认证通过不代表业务已启用。</p>
        <label>终端编号（可选）<input v-model="form.workbench_id" data-testid="ossbox-workbench" autocomplete="off" placeholder="部署方分配的终端编号" /></label>
        <label>OSS 上报地址（可选）<input v-model="form.endpoint" data-testid="ossbox-endpoint" autocomplete="off" placeholder="部署方提供的 OSS 域名或 HTTPS 地址" /></label>
        <label>OSS 业务目标桶（与 STS 认证无关）<input v-model="form.bucket" data-testid="ossbox-bucket" autocomplete="off" placeholder="部署方提供的私有桶名" /></label>
        </template>
      </fieldset>
      <div class="qt-row wrap">
        <a-button type="primary" html-type="submit" data-testid="ossbox-save" :disabled="locked || !!busy || missingWorkbenchName" :loading="busy === 'save'">保存公网上报</a-button>
        <a-button data-testid="ossbox-test" :disabled="locked || !!busy || missingWorkbenchName" :loading="busy === 'test'" @click="submit('test')">{{ relayMode ? '测试公网认证' : '测试 STS 认证' }}</a-button>
        <a-button v-if="!loaded && !busy" :disabled="session.gateVisible" @click="loadSettings">重新读取</a-button>
      </div>
    </form>
    <p v-if="note" role="status" data-testid="ossbox-note">{{ note }}</p>
    <p v-if="authenticated && relayMode" class="auth-result" role="status" data-testid="ossbox-auth-result">公网认证通过，仅验证待处理查询。{{ Number.isSafeInteger(authenticated.pending) && authenticated.pending! >= 0 ? `待处理：${authenticated.pending}。` : '' }}{{ institution || legacyRelayMode ? '此结果不代表文件上传或原生消息送达。' : '采集端仍待对接，尚未发布目录；此结果不代表上传或消息链路已通过。' }}</p>
    <p v-else-if="authenticated" class="auth-result" role="status" data-testid="ossbox-auth-result">STS 认证通过，未验证任何 OSS 桶权限；临时凭据有效期至 {{ authenticated.expiration }}。此结果不代表上传或消息链路已通过。</p>
    <template v-if="saved && relayMode && institution">
      <p data-testid="ossbox-save-status">{{ saved.applied ? '配置已由采集服务装载。' : '配置尚未由采集服务应用。' }}{{ saved.restart_required ? '保存版本与运行版本不同；需完成受控切换。' : '' }}</p>
      <section data-testid="institution-reporting-status">
        <p>机构文件通道：{{ institution.active ? '已运行' : '已停止' }} · 企点账号工作进程 {{ institution.account_workers }}</p>
        <p>最近目录上传：{{ institution.last_directory_at ? new Date(institution.last_directory_at * 1000).toLocaleString() : '尚无成功记录' }}。上传完成不代表内网已接收。</p>
        <p v-if="statusError" role="alert">{{ statusError }}</p>
        <p v-for="(code, component) in institution.errors" :key="component" role="alert">{{ component }}：{{ code }}</p>
        <p v-for="(presence, uid) in institution.native_presence" :key="uid">企点 {{ uid }}：{{ presence.state === 'online' && now - presence.confirmed_at < 60 ? '原生在线已核验' : presence.state === 'offline' ? '原生已离线' : '原生状态尚未确认' }} · {{ presence.code }}</p>
        <p v-for="(history, uid) in institution.history" :key="uid">企点 {{ uid }} 主库历史：读取 {{ history.scanned }}，提交 {{ history.published }}，附件 {{ history.media_bytes }} 字节{{ history.error ? '；'+history.error : '' }}。提交数量不等于内网入库数量。</p>
        <a-button data-testid="institution-refresh-status" :disabled="!!busy || session.gateVisible" @click="refreshRuntime">刷新运行状态</a-button>
      </section>
    </template>
    <template v-else-if="saved && legacyRelayMode">
      <p data-testid="ossbox-save-status">{{ saved.relay?.configured ? '已有保存配置。' : '尚未保存配置。' }}{{ saved.applied ? '配置已由采集服务装载。' : '配置尚未由采集服务应用。' }}{{ saved.restart_required ? '保存配置与运行版本不同，需受控重启后生效。' : '' }}</p>
    </template>
    <template v-else-if="saved && relayMode">
      <p data-testid="ossbox-save-status">{{ (saved.relay?.saved ?? saved.relay?.configured) ? (saved.relay?.credential_error ? '已有保存配置，但认证凭据暂不可用，请检查服务连接。' : '采集端配置已保存，待对接。') : '尚未保存配置。' }}当前状态：待对齐，尚未发布新目录，现有连接未切换。</p>
    </template>
    <template v-else-if="saved">
      <p data-testid="ossbox-save-status">{{ saved.data.saved ? '已有保存配置。' : '尚未保存配置。' }}{{ saved.restart_required ? '保存配置待重启 Agent 后生效。' : '' }}</p>
      <p data-testid="ossbox-business-status">当前采集服务：询价{{ saved.inquiry_configured ? '所需配置已齐' : '所需配置未齐，尚未启用' }}；目录上报{{ saved.catalog_configured ? '所需配置已齐' : '所需配置未齐，尚未启用' }}。新保存的配置在重启后应用，业务运行状态需另行验证。</p>
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
.check { flex-direction: row; align-items: flex-start; }
.check input { width: auto; margin-top: 3px; }
.secret-state { color: var(--qt-text-secondary); font-size: 12px; }
.optional-hint { margin-top: 8px; }
.auth-result { color: var(--qt-primary); }
</style>
