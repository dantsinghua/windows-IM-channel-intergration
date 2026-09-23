<script setup lang="ts">
/**
 * `P-SCREEN` 画面(01 §2.7.4):企点 WebCodecs 内嵌流 + 事件回注;微信窗口截图预览(只看不点);QQ 无画面。
 * `login_required` 下画面注入照常可用(R-06),但发消息类不在画面里。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { screen as T, SCREEN_PERF_ITEMS, SCREEN_TOOLS } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useUiStore } from '@/stores/ui'
import { accountsApi } from '@/api/client'
import { ScreenStream, type DecodePath, type StreamClosed, type StreamProfile } from '@/codec/stream'
import StateDot from '@/components/StateDot.vue'
import { STATE_CODES } from '@/i18n/zh-CN/codes'
import { normInContain } from './pointer'

const route = useRoute()
const router = useRouter()
const accounts = useAccountsStore()
const ui = useUiStore()

const focusId = ref<string>(String(route.params.id ?? ''))
const canvas = ref<HTMLCanvasElement | null>(null)
const stats = ref({ fps: 0, latencyMs: 0, decoder: 'hardware' as DecodePath, codec: '', connected: false })
const degradeMsg = ref('')
/** #34 关闭码分诊结果(4401/4400/4409/4410/4503 分别提示,不一律「连接失败」) */
const streamClosed = ref<StreamClosed | null>(null)
const shotUrl = ref('')
const shotOpen = ref(false)
const wechatSrc = ref('')
const wechatNotReady = ref(false)
const staticSrc = ref('')
let stream: ScreenStream | null = null
let previewTimer: ReturnType<typeof setInterval> | null = null
let visibilityOff: (() => void) | undefined

const streamable = computed(() => accounts.items.filter((a) => a.channel !== 'qq'))
const qqAccounts = computed(() => accounts.items.filter((a) => a.channel === 'qq'))
const focus = computed(() => (focusId.value ? accounts.byId[focusId.value] ?? null : null))
const isWechat = computed(() => focus.value?.channel === 'wechat')
const loginPhase = computed(() => focus.value?.state === 'login_required')
const notRunning = computed(() =>
  !!focus.value && ['stopped', 'disabled', 'created'].includes(focus.value.state))
const starting = computed(() =>
  !!focus.value && ['starting', 'provisioning', 'logging_in'].includes(focus.value.state))

const PROFILE_OF: Record<string, StreamProfile> = { focus30: 'focus', focus15: 'focus15', thumb10: 'thumb10' }

function draw(frame: VideoFrame): void {
  const c = canvas.value
  if (!c) { frame.close(); return }
  const ctx = c.getContext('2d')
  if (!ctx) { frame.close(); return }
  if (c.width !== frame.displayWidth) c.width = frame.displayWidth
  if (c.height !== frame.displayHeight) c.height = frame.displayHeight
  ctx.drawImage(frame, 0, 0)
  frame.close()
}

function stopAll(): void {
  stream?.stop()
  stream = null
  if (previewTimer) clearInterval(previewTimer)
  previewTimer = null
}

async function startFocus(): Promise<void> {
  stopAll()
  degradeMsg.value = ''
  const a = focus.value
  if (!a) return
  if (a.channel === 'wechat') {
    // 微信:2s 一帧窗口截图,不接收任何输入
    previewTimer = setInterval(() => void pollWechat(), 2000)
    void pollWechat()
    return
  }
  if (!['running', 'degraded', 'login_required', 'starting'].includes(a.state)) return
  streamClosed.value = null
  stream = new ScreenStream(a.id, PROFILE_OF[ui.perfProfile] ?? 'focus', {
    onHeader: () => undefined,
    onStats: (s) => { stats.value = { ...stats.value, ...s } },
    onFrame: draw,
    onFatal: (why) => {
      degradeMsg.value = why
    },
    onClosed: (info) => {
      streamClosed.value = info
      if (info.reason === 'stream_backend_missing') { stopAll(); return }
      // 4401 由 App.vue 的事件流横幅统一引导「重取令牌」;4409/4400 重试无意义,都停手。
      if (!info.retryable) stopAll()
    },
  })
  await stream.start()
}

function startStaticPreview(): void {
  if (previewTimer) clearInterval(previewTimer)
  previewTimer = setInterval(() => void pollStatic(), 2000)
}

async function pollStatic(): Promise<void> {
  if (!focus.value) return
  try {
    const shot = await accountsApi.screenshot(focus.value.id)
    if (!shot.blob) return
    if (staticSrc.value) URL.revokeObjectURL(staticSrc.value)
    staticSrc.value = URL.createObjectURL(shot.blob)
  } catch { /* 下一轮再试 */ }
}

async function pollWechat(): Promise<void> {
  if (!focus.value) return
  try {
    const v = (await window.qt?.wa.invoke('wechat.ui-visible', {})) as { visible?: boolean } | undefined
    if (v && v.visible === false) { wechatNotReady.value = true; return }
    const shot = await accountsApi.screenshot(focus.value.id)
    if (!shot.blob) { wechatNotReady.value = true; return }
    wechatNotReady.value = false
    if (wechatSrc.value) URL.revokeObjectURL(wechatSrc.value)
    wechatSrc.value = URL.createObjectURL(shot.blob)
  } catch {
    wechatNotReady.value = true
  }
}

/** 归一化坐标 (x/w, y/h) → 控制帧 touch;无 WS 时走 REST 兜底 */
const zoom = ref(100)
/** 按下时落在画面内的坐标。松手若在黑边上,仍用这个点收尾,避免划到屏幕边缘触发系统返回。 */
let pointerDown: { x: number; y: number } | null = null

function mediaSize(el: HTMLElement): { w: number; h: number } {
  if (el instanceof HTMLCanvasElement) return { w: el.width, h: el.height }
  if (el instanceof HTMLImageElement) return { w: el.naturalWidth, h: el.naturalHeight }
  return { w: 0, h: 0 }
}

function onPointer(e: PointerEvent, action: 'down' | 'move' | 'up'): void {
  const el = e.currentTarget as HTMLElement | null
  if (!el || isWechat.value) return
  const r = el.getBoundingClientRect()
  const { w, h } = mediaSize(el)
  const inside = normInContain(e.clientX, e.clientY, r, w, h)
  if (action === 'down') {
    pointerDown = inside
    if (!inside) return
  } else if (!pointerDown) {
    return
  }
  const point = inside ?? pointerDown
  if (!point) return
  if (action === 'up') pointerDown = null
  if (stream && stats.value.connected) stream.send({ type: 'touch', action, x: point.x, y: point.y, pointer: e.pointerId })
  else if (focus.value && action === 'up') void accountsApi.streamInput(focus.value.id, { type: 'tap', x: point.x, y: point.y })
}

const KEYCODE_OF: Record<string, string> = {
  Enter: 'ENTER', Backspace: 'DEL', Escape: 'ESCAPE',
}

function onKey(e: KeyboardEvent): void {
  if (isWechat.value || !stream) return
  if (e.key.length === 1) {
    e.preventDefault()
    stream.send({ type: 'text', text: e.key })
    return
  }
  const keycode = KEYCODE_OF[e.key]
  if (!keycode) return
  e.preventDefault()
  stream.send({ type: 'key', keycode, action: 'down' })
}

function onWheel(e: WheelEvent): void {
  if (isWechat.value || !stream) return
  stream.send({ type: 'scroll', x: 0.5, y: 0.5, dx: e.deltaX, dy: e.deltaY })
}

function tool(t: string): void {
  if (!stream) return
  if (t === 'back') stream.send({ type: 'key', keycode: 'BACK', action: 'down' })
  else if (t === 'home') stream.send({ type: 'key', keycode: 'HOME', action: 'down' })
  // 不发 keycode ROTATE:正在跑的 Agent 把它收成返回键,点一次就退一层。
  // 新进程认 type=rotate,但旧进程会把未知类型当成坏帧并断开画面,所以这里先不发。
  else if (t === 'rotate') return
  else if (t === 'shot') void takeShot()
  else if (t === 'keyboard') canvas.value?.focus()
}

/** #33:二进制 + `X-QT-Media-Id`/`X-QT-Sha256` 头 —— 存档时把这两个值一起显示,便于对账 */
const shotMeta = ref<{ mediaId: string | null; sha256: string | null }>({ mediaId: null, sha256: null })

async function takeShot(): Promise<void> {
  if (!focus.value) return
  const shot = await accountsApi.screenshot(focus.value.id)
  if (!shot.blob) return
  if (shotUrl.value) URL.revokeObjectURL(shotUrl.value)
  shotUrl.value = URL.createObjectURL(shot.blob)
  shotMeta.value = { mediaId: shot.mediaId, sha256: shot.sha256 }
  shotOpen.value = true
}

async function saveShot(): Promise<void> {
  if (!shotUrl.value) return
  const bytes = new Uint8Array(await (await fetch(shotUrl.value)).arrayBuffer())
  await window.qt?.files.saveAs(`${focus.value?.id}-${Date.now()}.png`, 'image/png', bytes)
}

function setPerf(p: string): void {
  if (p === 'retry-hw') { stream?.retryHardware(); return }
  ui.perfProfile = p as typeof ui.perfProfile
  stream?.setProfile(PROFILE_OF[p] ?? 'focus')
}

function selectThumb(id: string): void {
  focusId.value = id
  ui.focusedAccountId = id
  void router.replace(`/screen/${id}`)
}

watch(focusId, () => void startFocus())
watch(() => route.params.id, (v) => { if (v) focusId.value = String(v) })

onMounted(async () => {
  if (!accounts.items.length) await accounts.load()
  // 这里再调一次 startFocus 会和上面的 watch 各开一条 focus 流。
  // 后到的那条被服务端拒绝(同时只允许 1 个),页面就停在空白画布上。
  if (!focusId.value && streamable.value.length) {
    focusId.value = streamable.value[0].id
    return
  }
  await startFocus()
  // 窗口最小化/隐藏 → 全部 pause(§2.7.4 规格策略)
  visibilityOff = window.qt?.window.onVisibility((visible) => {
    if (visible) stream?.resume()
    else stream?.pause()
  })
})

onUnmounted(() => {
  stopAll()
  visibilityOff?.()
  if (shotUrl.value) URL.revokeObjectURL(shotUrl.value)
  if (wechatSrc.value) URL.revokeObjectURL(wechatSrc.value)
  if (staticSrc.value) URL.revokeObjectURL(staticSrc.value)
})
</script>

<template>
  <div class="screen">
    <aside class="thumbs">
      <div class="qt-section-title">账号</div>
      <div
        v-for="a in streamable"
        :key="a.id"
        class="thumb"
        :class="{ active: a.id === focusId, guide: a.state === 'login_required' }"
        :data-testid="T.thumb(a.id)"
        @click="selectThumb(a.id)"
      >
        <StateDot :state="a.state" :reason="a.state_reason" />
        <span class="qt-grow">{{ a.id }}</span>
        <span class="qt-small qt-muted">{{ a.channel === 'wechat' ? '截图 2s' : '540p@5fps' }}</span>
        <a-button
          v-if="['stopped', 'created', 'disabled'].includes(a.state)"
          size="small"
          :data-testid="T.thumbStart(a.id)"
          @click.stop="accountsApi.start(a.id)"
        >启动</a-button>
      </div>
      <div
        v-for="a in qqAccounts"
        :key="a.id"
        class="thumb qq"
        :data-testid="T.qqHint(a.id)"
        @click="router.push({ path: '/msg', query: { account_id: a.id } })"
      >
        {{ a.id }} —— 无画面,去消息页
      </div>
    </aside>

    <main class="focus">
      <template v-if="focus">
        <header class="qt-row">
          <StateDot :state="focus.state" :reason="focus.state_reason" />
          <strong class="qt-grow">{{ focus.id }} {{ focus.label }}</strong>
          <span v-if="focus.state_code" class="qt-warn qt-small">
            {{ STATE_CODES[focus.state_code]?.zh ?? focus.state_code }}
          </span>
        </header>

        <!-- R-06:登录态可在画面里输验证码 / 拖滑块 -->
        <div v-if="loginPhase && !isWechat" class="banner" :data-testid="T.loginHint">
          登录中:可在画面里输验证码/拖滑块。发消息类请去指令台(登录态置灰)。
          ⚠️ 密码与短信码不回显、不落任何前端日志。
        </div>

        <a-skeleton v-if="starting" active />
        <a-empty v-else-if="notRunning" description="未运行">
          <a-button type="primary" @click="accountsApi.start(focus.id)">启动</a-button>
        </a-empty>

        <!-- 微信:只看不点 -->
        <template v-else-if="isWechat">
          <p v-if="wechatNotReady" :data-testid="T.wechatNotready" class="qt-warn">微信窗口不可见</p>
          <img
            v-else-if="wechatSrc"
            class="wxpreview"
            :data-testid="T.wechatPreview"
            :src="wechatSrc"
            title="微信画面仅预览;操作请在微信窗口进行"
            alt="微信窗口预览"
          />
          <p v-if="focus.state_code === 'SCREEN_LOCKED'" class="qt-danger">系统已锁屏,发送不可用</p>
        </template>

        <!-- 企点:WebCodecs canvas + 事件回注;静态预览为第三档降级 -->
        <template v-else>
          <!--
            🔴 #34 关闭码分诊(backend-api-2 §6):4503 / 4409 / 4401 / 4400 各说各的,
            不一律「连接失败」;不可重试的一律停手,不无限重连。
            ⚠️ 01 §4 还没有这块提示的元素 id(关闭码表 01 §5.1 也缺 4409/4410/4503),已列给文档方。
          -->
          <div v-if="streamClosed && !streamClosed.retryable" class="banner crit">
            <strong>{{ streamClosed.text }}</strong>
            <span class="qt-small qt-muted">(关闭码 {{ streamClosed.code }} · {{ streamClosed.reason }})</span>
            <p v-if="streamClosed.reason === 'stream_backend_missing'" class="qt-small qt-muted">
              画面流执行体本期未装配,已停止重连。截图与事件回注同样不可用,请改用消息页操作。
            </p>
            <p v-else-if="streamClosed.reason === 'unauthorized'" class="qt-small qt-muted">
              请用顶部横幅的「重新取令牌」恢复,再回到本页。
            </p>
          </div>
          <div v-else-if="streamClosed && streamClosed.reason === 'focus_taken'" class="banner">
            {{ streamClosed.text }}
            <a-button size="small" @click="startFocus">再试一次</a-button>
          </div>

          <img
            v-if="stats.decoder === 'static' && staticSrc"
            class="wxpreview"
            :src="staticSrc"
            alt="静态预览"
            :style="{ width: zoom + '%' }"
            @pointerdown="(e) => onPointer(e, 'down')"
            @pointermove="(e) => onPointer(e, 'move')"
            @pointerup="(e) => onPointer(e, 'up')"
          />
          <canvas
            v-else
            ref="canvas"
            class="canvas"
            tabindex="0"
            :data-testid="T.canvas"
            :style="{ width: zoom + '%' }"
            @pointerdown="(e) => onPointer(e, 'down')"
            @pointermove="(e) => onPointer(e, 'move')"
            @pointerup="(e) => onPointer(e, 'up')"
            @keydown="onKey"
            @wheel.prevent="onWheel"
          />
          <div class="qt-row tools">
            <a-button v-for="t in SCREEN_TOOLS" :key="t" size="small" :data-testid="T.tool(t)" @click="tool(t)">
              {{ ({ back: '返回', home: '主页', rotate: '旋转', shot: '截图', keyboard: '键盘输入' } as Record<string, string>)[t] }}
            </a-button>
            <a-dropdown>
              <a-button size="small" :data-testid="T.perfMenu">性能 ▾</a-button>
              <template #overlay>
                <a-menu>
                  <a-menu-item v-for="p in SCREEN_PERF_ITEMS" :key="p" :data-testid="T.perf(p)" @click="setPerf(p)">
                    {{ ({ focus30: '720p@30fps', focus15: '720p@15fps', thumb10: '540p@10fps', 'retry-hw': '重试硬解' } as Record<string, string>)[p] }}
                  </a-menu-item>
                </a-menu>
              </template>
            </a-dropdown>
            <span v-if="degradeMsg" class="tag" :data-testid="T.degradeTag">{{ degradeMsg }}</span>
          </div>
          <div class="qt-row status qt-small qt-muted">
            <span :data-testid="T.statusDecoder">
              解码 {{ stats.decoder === 'hardware' ? '硬解' : stats.decoder === 'software' ? '软解' : '静态预览' }}
              {{ stats.codec }}
            </span>
            <span :data-testid="T.statusFps">{{ stats.fps }} fps</span>
            <span :data-testid="T.statusLatency">延迟 {{ stats.latencyMs }} ms</span>
            <span :data-testid="T.statusStreamDot" class="dot"
                  :style="{ background: stats.connected ? 'var(--qt-state-running)' : 'var(--qt-state-stopped)' }" />
            <label class="zoom">缩放
              <input type="range" min="40" max="160" v-model.number="zoom" />
              {{ zoom }}%
            </label>
          </div>
        </template>
      </template>
      <a-empty v-else description="没有可显示画面的账号" />
    </main>

    <a-modal v-model:open="shotOpen" title="截图" :footer="null" :data-testid="T.shotPreview">
      <img v-if="shotUrl" :src="shotUrl" class="shot" alt="截图" />
      <!-- 二进制响应的 media_id / sha256 只在响应头里(非 JSON 响应不带 trace_id 字段) -->
      <p v-if="shotMeta.sha256" class="qt-small qt-muted qt-mono">
        media_id {{ shotMeta.mediaId ?? '—' }} · sha256 {{ shotMeta.sha256.slice(0, 16) }}…
      </p>
      <a-button type="primary" :data-testid="T.shotSave" @click="saveShot">保存</a-button>
    </a-modal>
  </div>
</template>

<style scoped>
.screen { display: flex; height: 100%; }
.thumbs { width: 260px; flex: 0 0 auto; padding: var(--qt-space-3); border-right: 1px solid var(--qt-border); overflow: auto; }
.thumb { display: flex; align-items: center; gap: var(--qt-space-2); padding: 6px; cursor: pointer; border-radius: var(--qt-radius-sm); }
.thumb:hover { background: var(--qt-bg-elevated); }
.thumb.active { background: var(--qt-bg-elevated); border: 1px solid var(--qt-primary); }
.thumb.guide { box-shadow: 0 0 0 2px var(--qt-state-login_required) inset; }
.thumb.qq { color: var(--qt-text-disabled); }
.focus { flex: 1 1 auto; padding: var(--qt-space-3); overflow: auto; }
.banner { background: #FFFBE6; color: var(--qt-sev-warn); padding: 6px var(--qt-space-3); margin: var(--qt-space-2) 0; }
.banner.crit { background: #FFF1F0; color: var(--qt-sev-crit); }
.canvas, .wxpreview {
  width: 100%; max-width: 720px; max-height: 70vh; object-fit: contain;
  background: #000; display: block; outline: none; cursor: pointer;
}
.zoom { display: inline-flex; align-items: center; gap: 6px; }
.tools { margin-top: var(--qt-space-2); flex-wrap: wrap; }
.status { margin-top: var(--qt-space-2); gap: var(--qt-space-3); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.tag { color: var(--qt-sev-warn); border: 1px solid var(--qt-sev-warn); border-radius: 8px; padding: 0 6px; font-size: var(--qt-font-xs); }
.shot { max-width: 100%; margin-bottom: var(--qt-space-3); }
</style>
