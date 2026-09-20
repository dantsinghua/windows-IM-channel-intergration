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
  { path: '/cmd', name: 'P-CMD', component: () => import('@/pages/cmd/CmdPage.vue') },
  { path: '/flow', name: 'P-FLOW', component: () => import('@/pages/flow/FlowPage.vue') },
  { path: '/msg', name: 'P-MSG', component: () => import('@/pages/msg/MsgPage.vue') },
  { path: '/mail', name: 'P-MAIL', component: () => import('@/pages/mail/MailPage.vue') },
  { path: '/env', name: 'P-ENV', component: () => import('@/pages/env/EnvPage.vue') },
  { path: '/set', name: 'P-SET', component: () => import('@/pages/set/SetPage.vue') },
  // E-4 子页,仍属 P-SET
  { path: '/set/mail-templates', name: 'P-SET-MAILTPL', component: () => import('@/pages/set/MailTemplatesPage.vue') },
  { path: '/log', name: 'P-LOG', component: () => import('@/pages/log/LogPage.vue') },
]

export const router = createRouter({ history: createWebHashHistory(), routes })

/**
 * ① `setup.done===false` 一律重定向 `/setup`;
 * ② `qt.auth.state()!=='ok'` **显示门禁覆盖层而不是跳页**(保留原路由,恢复后原地继续)。
 */
export function installGuards(isSetupDone: () => boolean): void {
  router.beforeEach((to) => {
    if (!isSetupDone() && to.path !== '/setup') return { path: '/setup' }
    if (isSetupDone() && to.path === '/setup') return true
    return true
  })
}
