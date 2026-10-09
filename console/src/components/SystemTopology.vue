<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useEnvStore } from '@/stores/env'
import { useEventsStore } from '@/stores/events'
import { useSessionStore } from '@/stores/session'
import { dash as T } from '@/testids'
import QtIcon from './QtIcon.vue'
import { KERNEL_STATE_TEXT } from '@/i18n/zh-CN/codes'
const env = useEnvStore()
const session = useSessionStore()
const events = useEventsStore()
const nodes = computed(() => [
  { id: 'ws', label: '控制台', sub: 'REST / WebSocket', icon: 'screen', ok: events.connected, area: 'console' },
  { id: 'agent', label: 'QTrade Agent', sub: '账号 · 采集 · 调度', icon: 'flow', ok: env.health ? session.agentReachable : null, area: 'agent' },
  { id: 'sqlite', label: 'SQLite', sub: '消息 · 日志 · 状态', icon: 'env', ok: null, area: 'db' },
  { id: 'kernel', label: 'WSL 内核', sub: KERNEL_STATE_TEXT[env.version?.kernel_state ?? ''] ?? '内核状态未知', icon: 'cpu', ok: KERNEL_STATE_TEXT[env.version?.kernel_state ?? ''] ? env.version?.kernel_state === 'OURS' : null, area: 'kernel' },
  { id: 'docker', label: 'Docker', sub: env.version?.docker ?? '版本未知', icon: 'env', ok: env.health?.dockerd ?? null, area: 'docker' },
  { id: 'redroid', label: 'redroid / 企点', sub: '旁路读库 · 画面控制', icon: 'screen', ok: null, area: 'redroid' },
  { id: 'napcat', label: 'NapCat / QQ', sub: 'OneBot 收发消息', icon: 'msg', ok: null, area: 'napcat' },
  { id: 'winagent', label: 'WinAgent', sub: 'Windows 本机服务', icon: 'env', ok: env.health ? session.winagentOnline : null, area: 'winagent' },
  { id: 'winagent-user', label: '会话代理 / 微信', sub: 'Windows 登录会话', icon: 'acct', ok: env.health ? session.userAgentOnline : null, area: 'user' },
])
function stateText(ok: boolean | null): string { return ok === null ? '未独立采样' : ok ? '在线' : '异常' }

const grid = ref<HTMLElement | null>(null)
const size = ref({ width: 1, height: 1 })
const paths = ref<{ id: string; d: string; bidirectional?: boolean }[]>([])
let observer: ResizeObserver | undefined
let frame = 0
type Point = [number, number]

// Round each bend in pixel space so curves stay smooth at any window width.
function rounded(points: Point[]): string {
  let d = `M ${points[0][0]} ${points[0][1]}`
  for (let i = 1; i < points.length - 1; i++) {
    const [before, at, after] = [points[i - 1], points[i], points[i + 1]]
    const incoming = Math.hypot(at[0] - before[0], at[1] - before[1])
    const outgoing = Math.hypot(after[0] - at[0], after[1] - at[1])
    const radius = Math.min(24, incoming / 2, outgoing / 2)
    if (!incoming || !outgoing) continue
    const start = at.map((v, axis) => v + (before[axis] - v) * radius / incoming)
    const end = at.map((v, axis) => v + (after[axis] - v) * radius / outgoing)
    d += ` L ${start[0]} ${start[1]} Q ${at[0]} ${at[1]} ${end[0]} ${end[1]}`
  }
  const last = points[points.length - 1]
  return `${d} L ${last[0]} ${last[1]}`
}
function measure(): void {
  const host = grid.value
  if (!host || host.clientWidth < 1) return
  const bounds = host.getBoundingClientRect()
  size.value = { width: bounds.width, height: bounds.height }
  const box = (id: string) => {
    const r = host.querySelector<HTMLElement>(`[data-node="${id}"]`)!.getBoundingClientRect()
    return { left: r.left - bounds.left, right: r.right - bounds.left, top: r.top - bounds.top,
      bottom: r.bottom - bounds.top, x: r.left - bounds.left + r.width / 2, y: r.top - bounds.top + r.height / 2 }
  }
  const a = box('agent'), c = box('ws'), db = box('sqlite'), w = box('winagent')
  const r = box('redroid'), n = box('napcat'), u = box('winagent-user')
  const upper = a.top - 15, middle = (a.bottom + r.top) / 2, branch = r.left - 18
  paths.value = [
    { id: 'console-agent', d: `M ${c.right} ${c.y} C ${c.right + 15} ${c.y}, ${a.left - 15} ${a.y}, ${a.left} ${a.y}`, bidirectional: true },
    { id: 'agent-sqlite', d: `M ${a.right} ${a.y} C ${a.right + 15} ${a.y}, ${db.left - 15} ${db.y}, ${db.left} ${db.y}` },
    { id: 'agent-winagent', d: rounded([[a.x,a.top],[a.x,upper],[w.x,upper],[w.x,w.top]]) },
    { id: 'agent-redroid', d: rounded([[a.x,a.bottom],[a.x,middle],[branch,middle],[branch,r.y],[r.left,r.y]]) },
    { id: 'agent-napcat', d: rounded([[a.x,a.bottom],[a.x,middle],[branch,middle],[branch,n.y],[n.left,n.y]]) },
    { id: 'winagent-session', d: `M ${w.x} ${w.bottom} C ${w.x} ${w.bottom + 15}, ${u.x} ${u.top - 15}, ${u.x} ${u.top}` },
  ]
}
function scheduleMeasure(): void { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure) }
onMounted(() => {
  void nextTick().then(scheduleMeasure)
  if (typeof ResizeObserver !== 'undefined' && grid.value) {
    observer = new ResizeObserver(scheduleMeasure)
    observer.observe(grid.value)
  }
})
onUnmounted(() => { observer?.disconnect(); cancelAnimationFrame(frame) })
</script>
<template>
  <section class="topology qt-glass" aria-label="系统架构">
    <header class="topology-heading"><div><span class="qt-eyebrow">CONNECTED WORKSPACE</span><h2>系统架构</h2></div><a href="#/env">管理运行环境</a></header>
    <div class="topology-legend"><span>控制台 ⇄ Agent · REST / WebSocket</span><span>Agent → SQLite / 通道适配器 / WinAgent</span></div>
    <div ref="grid" class="topology-grid">
      <div class="environment-zone wsl-zone" aria-hidden="true" /><div class="environment-zone windows-zone" aria-hidden="true" />
      <div class="lane-label wsl-label"><i />WSL <span>Linux 运行环境</span></div><div class="lane-label win-label"><i />Windows <span>本机服务</span></div>
      <svg class="connections" :viewBox="`0 0 ${size.width} ${size.height}`" aria-hidden="true">
        <defs><linearGradient id="qt-link-tone" gradientUnits="userSpaceOnUse" x1="0" y1="0" :x2="size.width" y2="0"><stop stop-color="#b49bcf"/><stop offset="1" stop-color="#c5b7d5"/></linearGradient><marker id="qt-topology-arrow" markerWidth="5" markerHeight="5" refX="4" refY="2.5" orient="auto-start-reverse"><path d="M1 0.5 L3.5 2.5 L1 4.5" fill="none" stroke="#ae98c2" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>
        <g fill="none" stroke="url(#qt-link-tone)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path v-for="edge in paths" :key="edge.id" :data-edge="edge.id" :d="edge.d" marker-end="url(#qt-topology-arrow)" :marker-start="edge.bidirectional ? 'url(#qt-topology-arrow)' : undefined" />
        </g>
      </svg>
      <a v-for="n in nodes" :key="n.id" :data-node="n.id" :href="`#/env?section=${n.id}`" class="node" :class="[n.area,{central:n.id==='agent'}]" :data-testid="T.sys(n.id)"><span class="node-icon"><QtIcon :name="n.icon" :size="19" /></span><div><strong>{{ n.label }}</strong><small>{{ n.sub }}</small></div><span class="health" :class="{ ok:n.ok === true, bad:n.ok === false }" :title="stateText(n.ok)" :aria-label="stateText(n.ok)" /></a>
    </div>
    <p class="topology-note">Docker 承载 redroid 与 NapCat；WinAgent 经命名管道连接会话代理。连线表示代码关系，节点状态以最近采样为准。</p>
  </section>
</template>
<style scoped>
.topology{padding:26px 28px;position:relative;overflow:hidden;background:linear-gradient(120deg,#ffffffc9,#faf8fdbd 60%,#fff9eec2)}
.topology-heading{display:flex;align-items:center;justify-content:space-between;gap:16px}.topology-heading h2{font-size:21px;font-weight:600;margin:0}.topology-heading a{font-size:13px}.qt-eyebrow{font-size:12px;letter-spacing:2px;margin-bottom:5px}.topology-legend{display:flex;gap:24px;color:#887b95;font-size:12px;margin-top:14px}
.topology-grid{position:relative;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));grid-template-rows:48px 88px 42px 88px 24px 78px 14px;column-gap:12px;margin-top:22px;isolation:isolate}
.environment-zone{border-radius:22px;border:1px solid;z-index:0;pointer-events:none;box-shadow:inset 0 1px 0 #ffffffc9}.wsl-zone{grid-area:1/2/8/5;background:linear-gradient(135deg,#eae0f6aa,#f1e9fa70);border-color:#dfd0ed}.windows-zone{grid-area:1/5/8/6;background:linear-gradient(140deg,#f8e8c6ab,#fff4dc80);border-color:#efdbb7}
.lane-label{display:flex;align-items:center;gap:7px;z-index:2;padding:0 16px;align-self:start;margin-top:14px;font-size:12px;font-weight:600;color:#86649f;line-height:16px;white-space:nowrap}.lane-label span{font-weight:400;font-size:11px;color:#9985a9}.lane-label i{height:6px;width:6px;background:#ac87cc;border-radius:2px}.wsl-label{grid-area:1/2/2/5}.win-label{grid-area:1/5/2/6;color:#99742e}.win-label i{background:#d6ac5d}.win-label span{color:#ac9466}
.connections{position:absolute;inset:0;width:100%;height:100%;z-index:1;pointer-events:none}
.node{z-index:2;display:flex;align-items:center;gap:9px;padding:14px 11px;margin:0 12px;background:#ffffffcf;border:1px solid #fff;border-radius:16px;color:var(--qt-text);box-shadow:0 4px 14px #59387906;min-width:0;backdrop-filter:blur(14px);transition:box-shadow .16s,border-color .16s}.node:hover{border-color:#c2a6d8;box-shadow:0 7px 20px #7547a817}.node:focus-visible{outline:2px solid #8e66ae;outline-offset:3px}.node strong{font-size:13px;display:block;font-weight:600}.node small{display:block;font-size:11px;color:#8b8095;margin-top:5px;overflow-wrap:anywhere}.node-icon{display:grid;place-items:center;color:#967db0;flex-shrink:0}.central{background:linear-gradient(110deg,#8960b5,#704a9e);color:white;border-color:#a486c0;box-shadow:0 8px 20px #7547a824}.central small,.central .node-icon{color:#e7dcef}.health{margin-left:auto;flex-shrink:0;width:6px;height:6px;border-radius:50%;background:#c5bdce}.health.ok{background:#58b287;box-shadow:0 0 0 3px #58b28710}.health.bad{background:var(--qt-state-error)}
.console{grid-area:2/1;margin-left:0;margin-right:0}.agent{grid-area:2/2}.db{grid-area:2/3}.kernel{grid-area:4/2}.docker{grid-area:4/3}.redroid{grid-area:4/4}.napcat{grid-area:6/4}.winagent{grid-area:2/5}.user{grid-area:4/5}.topology-note{font-size:12px;color:#96879f;margin:16px 0 0;line-height:1.7}
@media(max-width:1300px){.node-icon{display:none}.node{padding:12px 10px;margin-left:10px;margin-right:10px}.console{margin:0}.node strong{font-size:12px}.win-label span{display:none}.topology-legend{flex-wrap:wrap;gap:6px}}
@media(max-width:900px){.topology{padding:22px}.topology-grid{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:78px 42px repeat(3,84px) 42px 84px;gap:12px 0}.connections{display:none}.wsl-zone{grid-area:2/1/6/3;margin-bottom:-6px}.windows-zone{grid-area:6/1/8/3;margin-bottom:-12px}.wsl-label{grid-area:2/1/3/3}.win-label{grid-area:6/1/7/3}.win-label span{display:inline}.node-icon{display:grid}.console{grid-area:1/1/2/3;max-width:240px;margin:0}.agent{grid-area:3/1}.db{grid-area:3/2}.kernel{grid-area:4/1}.docker{grid-area:4/2}.redroid{grid-area:5/1}.napcat{grid-area:5/2}.winagent{grid-area:7/1}.user{grid-area:7/2}.topology-note{margin-top:26px}.node small{font-size:12px}}
@media(max-width:460px){.topology{padding:18px 12px}.node{padding:12px 9px;margin:0 7px;gap:5px}.node-icon{display:none}.topology-heading a{font-size:12px}.lane-label{padding:0 12px}}
</style>
