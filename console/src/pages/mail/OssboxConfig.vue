<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ossboxApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import type { OssboxAuthenticationResult, OssboxSettingsInput, OssboxSettingsResponse } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const emit = defineEmits<{ saved: [] }>()
const session = useSessionStore()
const form = reactive({
  sts_url: '', auth_key: '', sm4_key: '', allow_http: false,
  endpoint: '', bucket: '', workbench_id: '',
})
const saved = ref<OssboxSettingsResponse | null>(null)
const loaded = ref(false)
const busy = ref<'' | 'load' | 'save' | 'test'>('')
const note = ref('')
const authenticated = ref<OssboxAuthenticationResult | null>(null)
const locked = computed(() => !loaded.value || session.gateVisible || session.draining || busy.value === 'save')
let revision = 0
let alive = true
let activeRequest: AbortController | null = null
let activeTimer: ReturnType<typeof setTimeout> | null = null

watch(form, () => {
  revision += 1
  authenticated.value = null
  note.value = ''
}, { deep: true, flush: 'sync' })

function errorText(error: unknown, operation: string): string {
  // 不显示服务端原始报错或请求内容，防止上游错误响应意外带回口令。
  if (error instanceof ApiFailure && error.status === 403) return '仅管理员可以配置和测试 STS 认证。'
  if (error instanceof ApiFailure && error.status === 401) return '管理会话已失效，请恢复连接后重试。'
  // Only display reviewed backend reason codes, never the upstream response or secret text.
  if (error instanceof ApiFailure && error.reason === 'sts_bucket_mismatch')
    return 'STS 已返回临时凭据，但签发桶名与页面的预期桶名不一致。请填写 STS 实际授权的桶名；仅验证认证时可暂时留空。不自动切换桶。'
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
  const body: OssboxSettingsInput = {
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
  if (!alive || busy.value || session.gateVisible) return
  const controller = new AbortController()
  activeRequest = controller
  busy.value = 'load'
  note.value = ''
  setRequestTimeout(controller, 20_000)
  try {
    const result = await ossboxApi.get(controller.signal)
    if (!alive || activeRequest !== controller || controller.signal.aborted) return
    saved.value = result
    Object.assign(form, {
      sts_url: result.data.sts_url, allow_http: result.data.allow_http,
      endpoint: result.data.endpoint, bucket: result.data.bucket, workbench_id: result.data.workbench_id,
      auth_key: '', sm4_key: '',
    })
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
      if (revision !== submittedRevision) {
        note.value = '请求中的配置已保存；页面上的后续修改尚未保存。重启 Agent 后生效。'
        return
      }
      form.auth_key = ''
      form.sm4_key = ''
      note.value = '配置已保存。密钥不回显；重启 Agent 后生效，页面不会自动重启服务。'
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
  authenticated.value = null
})

onMounted(() => { void loadSettings() })
onUnmounted(() => {
  alive = false
  activeRequest?.abort()
  if (activeRequest) finishRequest(activeRequest)
  form.auth_key = ''
  form.sm4_key = ''
})
</script>

<template>
  <article class="ossbox-card link-card qt-glass" data-testid="ossbox-config">
    <div class="qt-eyebrow">COLLECT</div>
    <h2>公网上报</h2>
    <p>填写部署方提供的 STS 地址和认证密钥。认证测试只申请临时凭据，不保存草稿、不上传消息，也不启用询价或群消息转发。</p>
    <p v-if="busy === 'load'" role="status">正在读取配置…</p>
    <form autocomplete="off" @submit.prevent="submit('save')">
      <fieldset :disabled="locked">
        <label>STS 接口地址<input v-model="form.sts_url" data-testid="ossbox-sts-url" type="url" autocomplete="off" placeholder="部署方提供的完整 STS 接口地址" /></label>
        <label class="check"><input v-model="form.allow_http" data-testid="ossbox-allow-http" type="checkbox" />允许 HTTP（仅测试环境，认证请求不加密传输）</label>
        <label>业务口令（auth.key）<input v-model="form.auth_key" data-testid="ossbox-auth-key" type="password" autocomplete="new-password" :placeholder="saved?.data.auth_key_configured ? '已配置；留空保留，输入新值替换' : '填写部署方提供的业务口令'" /></label>
        <span class="secret-state" data-testid="ossbox-auth-status">{{ saved?.data.auth_key_configured ? (form.auth_key ? '已有保存口令；本次输入尚未保存' : '业务口令已配置') : (form.auth_key ? '本次已输入，尚未保存' : '业务口令未配置') }}</span>
        <label>SM4 密钥（auth.sm4_key）<input v-model="form.sm4_key" data-testid="ossbox-sm4-key" type="password" autocomplete="new-password" :placeholder="saved?.data.sm4_key_configured ? '已配置；留空保留，输入新值替换' : 'UTF-8 编码后必须为 16 字节'" /></label>
        <span class="secret-state" data-testid="ossbox-sm4-status">{{ saved?.data.sm4_key_configured ? (form.sm4_key ? '已有保存密钥；本次输入尚未保存' : 'SM4 密钥已配置') : (form.sm4_key ? '本次已输入，尚未保存' : 'SM4 密钥未配置') }}</span>
        <p class="optional-hint">以下业务配置可稍后填写；认证通过不代表业务已启用。</p>
        <label>终端编号（可选）<input v-model="form.workbench_id" data-testid="ossbox-workbench" autocomplete="off" placeholder="部署方分配的终端编号" /></label>
        <label>OSS 上报地址（可选）<input v-model="form.endpoint" data-testid="ossbox-endpoint" autocomplete="off" placeholder="部署方提供的 OSS 域名或 HTTPS 地址" /></label>
        <label>预期桶名（可选，需与 STS 签发桶一致）<input v-model="form.bucket" data-testid="ossbox-bucket" autocomplete="off" placeholder="部署方提供的私有桶名" /></label>
      </fieldset>
      <div class="qt-row wrap">
        <a-button type="primary" html-type="submit" data-testid="ossbox-save" :disabled="locked || !!busy" :loading="busy === 'save'">保存公网上报</a-button>
        <a-button data-testid="ossbox-test" :disabled="locked || !!busy" :loading="busy === 'test'" @click="submit('test')">测试 STS 认证</a-button>
        <a-button v-if="!loaded && !busy" :disabled="session.gateVisible" @click="loadSettings">重新读取</a-button>
      </div>
    </form>
    <p v-if="note" role="status" data-testid="ossbox-note">{{ note }}</p>
    <p v-if="authenticated" class="auth-result" role="status" data-testid="ossbox-auth-result">认证通过。返回桶名：{{ authenticated.bucket }}；凭据有效期至 {{ authenticated.expiration }}。此结果不代表上传或消息链路已通过。</p>
    <template v-if="saved">
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
