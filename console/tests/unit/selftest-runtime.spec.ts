import { describe, expect, it } from 'vitest'
import { selftestRows, type SelftestRun } from '@/api/types'

describe('日常自检不把已在跑的 NapCat 写成没有执行器', () => {
  it('NapCat 运行中为正常，日常检查不列出一次性安装容器，账号容器单独列出', () => {
    const run: SelftestRun = {
      napcat_ok: true,
      napcat_detail: 'qq92 运行中',
      redroid_boot_ms: null,
      account_containers: [
        { id: 'qq92', channel: 'qq', running: true },
        { id: 'qd90', channel: 'qidian', running: true },
      ],
      skipped: [{ step: 'redroid_boot', reason: 'install_only' }],
    }
    const rows = selftestRows(run)
    const napcat = rows.find((row) => row.item === 'napcat')
    const boot = rows.find((row) => row.item === 'redroid_boot')
    const boxes = rows.find((row) => row.item === 'account_containers')
    expect(napcat?.level).toBe('ok')
    expect(napcat?.message).toBe('qq92 运行中')
    expect(boot).toBeUndefined()
    expect(rows.map((row) => row.item)).not.toContain('redroid_boot')
    expect(boxes?.level).toBe('ok')
    expect(boxes?.message).toContain('qq92 运行中')
    expect(boxes?.message).toContain('qd90 运行中')
  })

  it('没有任何账号容器时，原因写成中文而不是 no_executor', () => {
    const rows = selftestRows({
      napcat_ok: null,
      redroid_boot_ms: null,
      skipped: [
        { step: 'redroid_boot', reason: 'no_executor' },
        { step: 'napcat', reason: 'no_executor' },
      ],
    })
    expect(rows.find((entry) => entry.item === 'redroid_boot')).toBeUndefined()
    const napcat = rows.find((entry) => entry.item === 'napcat')
    expect(napcat?.level).toBe('warn')
    expect(napcat?.message).toBe('没有可调用的检查程序')
    expect(napcat?.message).not.toBe('no_executor')
  })
})
