/**
 * `qt.wa.invoke(op, args)` 的 **op 白名单**(01 §2.5,R-07/§11.19 [CONSOLESEC])。
 *
 * 渲染进程**一律禁止**发往 17610(netguard 已整体阻断);需要 WinAgent 能力时只能交这些 op
 * 给主进程持令牌代调。写/删 Vault 的 op 额外弹 `dialog.showMessageBox` 原生模态。
 *
 * ⚠️ `wechat/login/cancel` **不在此列** —— 取消登录一律走 Agent 级
 * `POST /api/v1/accounts/{id}/login/cancel {login_session_id}`(R4-4/§11.18)。
 */

export interface WaOp {
  method: 'GET' | 'POST' | 'PUT' | 'DELETE'
  /** 路径模板;`:x` 段从 args 取 */
  path: string
  /** 写/删 Vault → 主进程原生模态确认 */
  nativeConfirm?: { title: string; message: string }
}

export const WA_WHITELIST: Record<string, WaOp> = {
  // ② 保险库(P-SET 保险库区块);**没有 read** —— 控制台令牌调 read 恒 403
  'vault.list': { method: 'GET', path: '/wa/v1/vault' },
  'vault.put': {
    method: 'PUT',
    path: '/wa/v1/vault/:id',
    nativeConfirm: { title: '更新保险库条目', message: '将写入 WinAgent 保险库(DPAPI)。确认继续?' },
  },
  'vault.delete': {
    method: 'DELETE',
    path: '/wa/v1/vault/:id',
    nativeConfirm: { title: '删除保险库条目', message: '将从 WinAgent 保险库中删除该条目,不可恢复。确认继续?' },
  },

  // ③ 微信模块设置 + 登录流人工动作 + 登录流只读状态
  'wechat.settings.get': { method: 'GET', path: '/wa/v1/settings/wechat' },
  'wechat.settings.put': { method: 'PUT', path: '/wa/v1/settings/wechat' },
  'wechat.status': { method: 'GET', path: '/wa/v1/wechat/status' },
  'wechat.update-block': { method: 'POST', path: '/wa/v1/wechat/update-block' },
  'wechat.key.retry': { method: 'POST', path: '/wa/v1/wechat/key/retry' },
  'wechat.reinstall': { method: 'POST', path: '/wa/v1/wechat/reinstall' },
  'wechat.login.status': { method: 'GET', path: '/wa/v1/wechat/login/status' },
  'wechat.ui-visible': { method: 'GET', path: '/wa/v1/wechat/ui-visible' },

  // ④⑤ .wslconfig / wsl 动作
  'wsl.config.get': { method: 'GET', path: '/wa/v1/wsl/config' },
  'wsl.config.put': { method: 'PUT', path: '/wa/v1/wsl/config' },
  'wsl.start': { method: 'POST', path: '/wa/v1/wsl/start' },
  'wsl.kernel.apply': { method: 'POST', path: '/wa/v1/wsl/kernel/apply' },
  'wsl.kernel.rollback': { method: 'POST', path: '/wa/v1/wsl/kernel/rollback' },
  'wsl.distro.repair': { method: 'POST', path: '/wa/v1/wsl/distro/repair' },
  'firewall.ensure': { method: 'POST', path: '/wa/v1/firewall/ensure' },
  'power.keepawake': { method: 'POST', path: '/wa/v1/power/keepawake' },

  // ⑥ Agent 不可达时的一句话状态(04 §2.4.3 唯一例外)
  'wa.health': { method: 'GET', path: '/wa/v1/health' },
}

export function resolvePath(op: WaOp, args: Record<string, unknown>): { path: string; body?: unknown } {
  let path = op.path
  const rest: Record<string, unknown> = { ...args }
  for (const seg of op.path.split('/')) {
    if (!seg.startsWith(':')) continue
    const key = seg.slice(1)
    const v = rest[key]
    if (v === undefined) throw new Error(`缺少参数 ${key}`)
    path = path.replace(seg, encodeURIComponent(String(v)))
    delete rest[key]
  }
  if (op.method === 'GET' || op.method === 'DELETE') return { path }
  return { path, body: rest }
}
