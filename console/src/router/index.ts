/** 路由 = 页面 ID(01 §2.5) */
import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/dash' },
  { path: '/setup', name: 'P-SETUP', component: () => import('@/pages/setup/SetupPage.vue') },
  { path: '/dash', name: 'P-DASH', component: () => import('@/pages/dash/DashPage.vue') },
  { path: '/res', name: 'P-RES', component: () => import('@/pages/res/ResPage.vue') },
  { path: '/acct', name: 'P-ACCT', component: () => import('@/pages/acct/AcctPage.vue') },
  { path: '/acct/new', name: 'P-ACCT-NEW', component: () => import('@/pages/acct/AcctNewPage.vue') },
  { path: '/acct/:id', name: 'P-ACCT-DETAIL', component: () => import('@/pages/acct/AcctDetailPage.vue'), props: true },
  { path: '/screen/:id?', name: 'P-SCREEN', component: () => import('@/pages/screen/ScreenPage.vue'), props: true },
  { path: '/cmd', name: 'P-CMD', redirect: '/dash' },
  { path: '/flow', name: 'P-FLOW', redirect: '/dash' },
  { path: '/msg', name: 'P-MSG', component: () => import('@/pages/msg/MsgPage.vue') },
  { path: '/mail', name: 'P-MAIL', component: () => import('@/pages/mail/MailPage.vue') },
  { path: '/env', name: 'P-ENV', component: () => import('@/pages/env/EnvPage.vue') },
  { path: '/set', name: 'P-SET', component: () => import('@/pages/set/SetPage.vue') },
  // E-4 子页,仍属 P-SET
  { path: '/set/mail-templates', name: 'P-SET-MAILTPL', redirect: '/set' },
  { path: '/log', name: 'P-LOG', component: () => import('@/pages/log/LogPage.vue') },
]

export const router = createRouter({ history: createWebHashHistory(), routes })

/**
 * ① `setup.done===false` 一律重定向 `/setup`,**只有一条窄例外**(见下);
 * ② `qt.auth.state()!=='ok'` **显示门禁覆盖层而不是跳页**(保留原路由,恢复后原地继续)。
 * 🔴 R6-84(2026-10-10 安琳):原「告知页改版按住 `/setup` 重勾」(05 §6.1 末句)随「阅读须知」步一起退役,
 * 守卫不再看告知确认。
 *
 * 🔴 窄例外:向导步 3「首登」的「现在添加」要进 `P-ACCT-NEW`(01 §2.7.1 步 3,**完成后回到本步**)。
 * 不开例外则这个按钮被本守卫静默打回 `/setup`(同址导航、无报错)= 死按钮,首登引导只剩「跳过」。
 * 例外**只**认「未完成向导 + 目标恰是 `P-ACCT-NEW` + 来自向导(`?from=setup`)」三条同时成立,
 * 借它进别的页一概不放行;建号页上的「返回列表 / 取消」跳 `/acct` 时照旧被打回 `/setup`,
 * 而向导步存在 store 里(`stores/setup.ts` 的 `step`)⇒ 回去就是原来的第 3 步。
 */
/** 守卫判据本体(纯函数,便于单测):返回 `null` = 放行,否则是重定向目标 */
export function setupRedirect(
  to: { path: string; name?: unknown; query: Record<string, unknown> },
  gate: { done: boolean },
): { path: string } | null {
  if (gate.done) return null
  if (to.path === '/setup') return null
  if (to.name === 'P-ACCT-NEW' && to.query.from === 'setup') return null
  return { path: '/setup' }
}

export function installGuards(isSetupDone: () => boolean): void {
  router.beforeEach((to) => {
    const target = setupRedirect(
      { path: to.path, name: to.name, query: to.query as Record<string, unknown> },
      { done: isSetupDone() },
    )
    return target ?? true
  })
}
