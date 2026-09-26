/** `env` store:重跑自检前先清掉上一轮的错误(评审 C 节 console:`env.ts` 开跑前不清 `error`) */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { systemApi } from '@/api/client'
import { useEnvStore } from '@/stores/env'

beforeEach(() => {
  setActivePinia(createPinia())
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('runSelftest', () => {
  it('上一轮留下的 error 在这一轮开跑时就清掉,这轮成功后不再挂着', async () => {
    const env = useEnvStore()
    env.error = '自检结果还没写完,请再点一次'
    let seenAtStart: string | null | undefined
    vi.spyOn(systemApi, 'selftestRun').mockImplementation(async () => {
      seenAtStart = env.error
      return { run_id: 'r1' }
    })
    vi.spyOn(systemApi, 'selftestResult').mockResolvedValue({
      items: [], run: { run_id: 'r1', finished_at: '2026-09-26T06:00:00Z' }, runId: 'r1',
    } as unknown as Awaited<ReturnType<typeof systemApi.selftestResult>>)
    await env.runSelftest()
    expect(seenAtStart).toBeNull()
    expect(env.error).toBeNull()
  })
})
